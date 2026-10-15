"""Cadastro, edição e exclusão de produtos. Há dois pontos de escrita: `cadastrar` e `apagar`.

A fonte da verdade é o arquivo ghost/catalogo/produtos/<id>.json. Tudo que é do produto fica nele: preço, ordem,
canal, modelo de entrega, mensagem do carrinho, opção do menu (select) e o bloco da vitrine onde ele aparece.

    {
      "id": "netflix_fa", "nome": "Netflix FA", "preco": 11.6, "ordem": 1, "ativo": true, "anunciar": true,
      "canal": null, "imagem": "https://...", "garantia": "7 dias após a entrega.",
      "select": {"rotulo": "Netflix FA", "descricao": "{preco} | {estado}", "emoji": "<:netflix:123>"},
      "modelo": {"estoque": "conta", "entrega": "automatica", "sem_estoque": "avisar", "pagamento": "...",
                 "pagamento_manual": "..."},
      "mensagem": {"automatico": [...], "manual": [...]},
      "vitrine": {"id": "FA", "tipo": "selecao", "titulo": "...", "descricao": "...", ...}
    }

- A conferência é feita por catalogo.normalizar(). Se houver erro, nada é gravado.
- Se o ID já existe, o produto é atualizado. Sem ID, ele sai do nome.
- Se nada mudou em relação ao arquivo, nada é gravado e a vitrine não é mexida.
- Se o catálogo recusar o arquivo novo depois de gravado, o arquivo anterior volta.
- A vitrine é montada a partir do produto ativo de menor ordem dentro dela (ver catalogo). Por isso, qualquer
  mudança no produto atualiza a vitrine dele.
"""

import json
import logging
from dataclasses import dataclass, field

import discord

from .. import catalogo
from ..data import stock, store
from . import avisos, shelves

log = logging.getLogger("produtos")

LIMITE_ARQUIVO = 512 * 1024


def _exemplo() -> dict:
    """Modelo para começar um produto novo. Vem como rascunho (ativo false, vitrine não publicada): se for
    salvo sem mudanças, não aparece para ninguém até a equipe ativar."""
    return {
        "id": "meu_produto",
        "nome": "Meu produto",
        "preco": 10.0,
        "ordem": 1,
        "ativo": False,
        "anunciar": False,
        "canal": None,
        "imagem": "https://exemplo.com/produto.png",
        "garantia": "7 dias após a entrega.",
        "select": {
            "rotulo": "Meu produto",
            "descricao": "{preco} | {estado}",
            "emoji": "@cardbox",
        },
        "modelo": catalogo.padrao("conta")["modelo"],
        "mensagem": catalogo.padrao("conta")["mensagem"],
        "vitrine": {
            "id": "MEU_PRODUTO",
            "tipo": "selecao",
            "nome_setup": "Meu produto",
            "publicar": False,
            "titulo": "Título da vitrine",
            "descricao": "Texto da vitrine.",
            "cor": 16777215,
            "imagem": "https://exemplo.com/vitrine.png",
            "custom_id": "select_meu_produto",
            "placeholder": "Selecione um produto...",
        },
    }


EXEMPLO = _exemplo()


@dataclass
class Relatorio:
    produto_id: str | None = None
    atualizado: bool = False
    sem_mudancas: bool = False
    apagado: bool = False
    erros: list[str] = field(default_factory=list)
    vitrine_salva: str | None = None  # vitrine do produto, quando ele foi gravado
    vitrine_atualizada: str | None = None  # vitrine publicada ou atualizada no Discord
    modelo_salvo: str | None = (
        None  # o modelo agora é parte do produto; este campo fica sempre vazio
    )
    avisos: list[str] = field(default_factory=list)

    def texto(self) -> str:
        if self.erros:
            return "**Nada foi gravado.**\n" + "\n".join(f"- {e}" for e in self.erros)
        if self.apagado:
            linhas = [f"**Produto apagado:** `{self.produto_id}`"]
        elif self.sem_mudancas:
            return f"**Nada mudou** em `{self.produto_id}`: o JSON é igual ao que já estava salvo."
        else:
            verbo = "atualizado" if self.atualizado else "cadastrado"
            linhas = [f"**Produto {verbo}:** `{self.produto_id}`"]
            if self.vitrine_salva:
                linhas.append(f"**Vitrine:** `{self.vitrine_salva}`")
        if self.vitrine_atualizada:
            linhas.append(f"**Vitrine atualizada:** `{self.vitrine_atualizada}`")
        linhas.extend(f"-# {aviso}" for aviso in self.avisos)
        return "\n".join(linhas)


def ler(bruto: bytes) -> dict:
    """Lê o arquivo. Levanta ValueError com uma mensagem para a equipe."""
    if len(bruto) > LIMITE_ARQUIVO:
        raise ValueError("o arquivo passa de 512 KB")
    try:
        dados = json.loads(bruto.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError(f"JSON inválido: {exc}") from None
    if not isinstance(dados, dict):
        raise ValueError("o arquivo precisa ser um objeto JSON de produto")
    return dados


def exportar(produto_id: str) -> dict:
    """Documento completo do produto, como está no arquivo. Vazio se o produto não existe."""
    return catalogo.doc_produto(produto_id) or {}


def _trocar(doc: dict | None, antes: dict | None) -> list[str]:
    """Põe `doc` no lugar do arquivo (None apaga) e recarrega o catálogo. Se o catálogo recusar, o arquivo
    anterior volta. Devolve os erros; lista vazia quando deu certo. Não há await aqui: a troca não se mistura
    com outra gravação."""
    try:
        if doc is None:
            catalogo.remover(antes["id"])
        else:
            catalogo.gravar(doc)
    except (OSError, catalogo.CatalogoInvalido) as exc:
        return [f"não foi possível gravar o arquivo: {exc}"]
    erros = store.recarregar_catalogo()
    if erros:
        try:
            if antes is None:
                catalogo.remover(doc["id"])
            else:
                catalogo.gravar(antes)
        except (OSError, catalogo.CatalogoInvalido):
            log.exception(
                "não foi possível restaurar o arquivo do produto %s",
                (antes or doc)["id"],
            )
        store.recarregar_catalogo()
    return erros


def _vitrine(rel: Relatorio, vid: str, erro: str | None) -> None:
    if erro:
        rel.avisos.append(f"vitrine {vid} não atualizada: {erro}")
        return
    v = catalogo.vitrine(vid)
    if v is not None and v.publicar:
        rel.vitrine_atualizada = vid


async def cadastrar(dados: dict, guild: discord.Guild | None, por_id: int) -> Relatorio:
    """Cria ou atualiza um produto. `guild` e `por_id` ficam para registro e para a chamada da camada de bot.
    Nada é gravado se houver erro."""
    pid = dados.get("id") or catalogo.gerar_id(str(dados.get("nome") or ""))
    if not isinstance(pid, str):
        return Relatorio(erros=["'id' precisa ser texto"])
    try:
        doc = catalogo.normalizar({**dados, "id": pid}, f"{pid}.json")
    except catalogo.CatalogoInvalido as exc:
        return Relatorio(produto_id=pid, erros=[str(exc)])

    antes = catalogo.doc_produto(pid)
    rel = Relatorio(produto_id=pid, atualizado=antes is not None)
    if antes is not None and antes["modelo"]["estoque"] != doc["modelo"]["estoque"]:
        from ..database.connection import connection

        with connection() as conn:
            if any(
                conn.execute(
                    f"SELECT 1 FROM {table} WHERE produto_key=? LIMIT 1", (pid,)
                ).fetchone()
                for table in ("estoque", "estoque_links")
            ):
                rel.erros = [
                    "não troque o tipo de estoque enquanto houver itens; esvazie-o primeiro"
                ]
                return rel
    if antes == doc:
        rel.sem_mudancas = True
        return rel
    rel.erros = _trocar(doc, antes)
    if rel.erros:
        return rel

    vid = doc["vitrine"]["id"]
    rel.vitrine_salva = vid
    if antes is not None and antes["vitrine"]["id"] != vid:
        _vitrine(
            rel,
            antes["vitrine"]["id"],
            await shelves.sincronizar(antes["vitrine"]["id"]),
        )
    _vitrine(rel, vid, await shelves.sincronizar(vid))

    if doc["ativo"] and doc["anunciar"]:
        try:
            await avisos.anunciar_novidades()
        except (RuntimeError, discord.HTTPException):
            log.exception("falha ao anunciar novidades de %s", pid)
            rel.avisos.append("novidade não anunciada (veja o log)")
    return rel


async def apagar(pid: str, guild: discord.Guild | None, por_id: int) -> Relatorio:
    """Apaga o produto: arquivo JSON, estoque e a parte dele na vitrine. A vitrine some se era o último produto."""
    antes = catalogo.doc_produto(pid)
    if antes is None:
        return Relatorio(produto_id=pid, erros=[f"o produto `{pid}` não existe"])
    rel = Relatorio(produto_id=pid)
    from ..database.connection import connection

    with connection() as conn:
        if conn.execute(
            "SELECT 1 FROM pedidos WHERE produto_key=? AND (status='pending' OR (status='approved' AND entrega_status IN ('pending','processing','manual'))) LIMIT 1",
            (pid,),
        ).fetchone():
            rel.erros = [
                "há pedidos em andamento; conclua-os antes de apagar o produto"
            ]
            return rel
    rel.erros = _trocar(None, antes)
    if rel.erros:
        return rel
    # O produto já saiu do catálogo: a tabela de estoque vem do documento apagado, não do catálogo.
    stock.clear(pid, link=antes["modelo"]["estoque"] == "link")
    rel.apagado = True
    vid = antes["vitrine"]["id"]
    _vitrine(rel, vid, await shelves.sincronizar(vid))
    return rel
