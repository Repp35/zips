"""[ dono ] Produto: adicionar, editar, apagar e listar produtos, e o botão Help.

Cada produto é um JSON no mesmo formato dos arquivos de ghost/catalogo/produtos. Tudo que é salvo aqui passa por
services.produtos.cadastrar, que valida antes e não grava nada se houver erro. Apagar passa por
services.produtos.apagar (arquivo, estoque e mensagem da vitrine).
"""

import inspect
import json
import logging

import discord

from ... import catalogo
from ...ui import layout
from ...catalogo import render
from ...data import store
from ...ui.emojis import E
from ...services import notify, produtos, rest
from ...config.settings import CORES
from . import (
    aviso,
    confirmar,
    navegacao,
    pagina_de,
    painel,
    rodape_pagina,
    seguir,
    seletor_opcoes,
    so_dono,
    sem_acao,
    voltar,
)

log = logging.getLogger("secao.produto")

LIMITE_CAMPO = 4000  # o Discord aceita até 4000 caracteres por campo de modal
MAX_CAMPOS = 5  # JSON de até 20 mil caracteres na tela de edição
MODELO_NOVO = {  # propositalmente incompleto: se for salvo sem editar, a validação recusa
    "id": "novo_produto",
    "nome": "Nome do produto",
    "preco": 0,
    "vitrine": {"id": "ID_DA_VITRINE"},
    "modelo": {"estoque": "conta"},
    "ativo": False,
    "anunciar": True,
    "garantia": "7 dias após a entrega.",
}
_RASCUNHOS: dict[
    int, tuple[str, str | None]
] = {}  # último JSON recusado de cada pessoa, para reabrir


# ── Modal de JSON ─────────────────────────────────────────────────────────────


def _partes(texto: str, tamanho: int = LIMITE_CAMPO - 100) -> list[str]:
    """Divide o texto em pedaços que cabem num campo, sempre na quebra de linha (juntar com \n restaura o texto)."""
    partes, atual = [], ""
    for linha in texto.split("\n"):
        if atual and len(atual) + 1 + len(linha) > tamanho:
            partes.append(atual)
            atual = linha
        else:
            atual = f"{atual}\n{linha}" if atual else linha
    partes.append(atual)
    return partes


class DocumentoModal(discord.ui.Modal):
    """Tela de texto com o JSON. O texto vai em até MAX_CAMPOS campos e os campos são juntados ao enviar."""

    def __init__(self, titulo: str, texto: str, ao_enviar):
        super().__init__(title=titulo[:45])
        self.ao_enviar = ao_enviar
        self.campos: list[discord.ui.TextInput] = []
        partes = _partes(texto)
        for i, parte in enumerate(partes, start=1):
            campo = discord.ui.TextInput(
                label=f"JSON do produto ({i}/{len(partes)})"[:45],
                style=discord.TextStyle.paragraph,
                default=parte,
                required=i == 1,
                max_length=LIMITE_CAMPO,
            )
            self.add_item(campo)
            self.campos.append(campo)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        from ..security import authorize

        if not await authorize(interaction, "owner"):
            return
        await self.ao_enviar(
            interaction, "\n".join(campo.value for campo in self.campos)
        )


async def _abrir(
    interaction: discord.Interaction, titulo: str, texto: str, ao_enviar
) -> None:
    if len(_partes(texto)) > MAX_CAMPOS or any(
        len(p) > LIMITE_CAMPO for p in _partes(texto)
    ):
        await interaction.response.send_message(
            f"{E.wrong} Este JSON passa do limite da tela de edição ({MAX_CAMPOS * LIMITE_CAMPO} caracteres).",
            ephemeral=True,
        )
        return
    await interaction.response.send_modal(DocumentoModal(titulo, texto, ao_enviar))


EXEMPLO_COMPLETO = {  # todos os campos que o produto aceita; ative e anuncie depois de preencher
    "id": "meu_produto",
    "nome": "Nome do produto",
    "preco": 10.0,
    "ordem": 1,
    "ativo": False,
    "anunciar": False,
    "canal": None,
    "imagem": "https://exemplo.com/produto.png",
    "garantia": "7 dias após a entrega.",
    "select": {
        "rotulo": "Nome do produto",
        "descricao": "{preco} | {estado}",
        "emoji": None,
    },
    "botao": {"label": "Comprar", "style": 2, "emoji": "@cart"},
    "sem_estoque": "manual",
    "modelo": {
        "estoque": "conta",
        "entrega": "automatica",
        "sem_estoque": "avisar",
        "pagamento": "Pagamento via PIX — entrega automática após confirmação.",
        "pagamento_manual": "Pagamento via PIX — entrega manual realizada por um membro da equipe.",
    },
    "mensagem": {
        "automatico": [
            {
                "tipo": "texto",
                "texto": "## {titulo}\n\n{valor}{cupom}\n**Em estoque:** {estoque}\n\n{pagamento}{garantia}",
            },
            {"tipo": "linha"},
            {"tipo": "imagem"},
            {"tipo": "linha"},
            {
                "tipo": "botoes",
                "itens": [
                    {
                        "acao": "pagar",
                        "label": "Pagar agora",
                        "style": 3,
                        "emoji": "@correct",
                    },
                    {
                        "acao": "lote",
                        "label": "Lote ({quantidade}x)",
                        "style": 2,
                        "emoji": "@cardbox",
                    },
                    {
                        "acao": "cupom",
                        "label": "Cupom",
                        "label_com_cupom": "Cupom: {cupom_codigo}",
                        "style": 2,
                        "emoji": "@ticket",
                    },
                    {
                        "acao": "cancelar",
                        "label": "Cancelar",
                        "style": 4,
                        "emoji": "@delete",
                    },
                ],
            },
        ],
        "manual": [
            {
                "tipo": "texto",
                "texto": "## {titulo}\n\n**Valor:** {preco}\n\n{pagamento}",
            },
            {"tipo": "linha"},
            {"tipo": "imagem"},
            {"tipo": "linha"},
            {
                "tipo": "botoes",
                "itens": [
                    {
                        "acao": "pagar",
                        "label": "Pagar agora",
                        "style": 3,
                        "emoji": "@correct",
                    },
                    {
                        "acao": "cancelar",
                        "label": "Cancelar",
                        "style": 4,
                        "emoji": "@delete",
                    },
                ],
            },
        ],
    },
    "vitrine": {
        "id": "MEU_PRODUTO",
        "tipo": "selecao",
        "nome_setup": "Nome do produto",
        "publicar": False,
        "custom_id": "select_meu_produto",
        "placeholder": "Selecione um produto...",
        "imagem": "https://exemplo.com/vitrine.png",
        "titulo": "Título da vitrine",
        "descricao": "Texto da vitrine.",
        "cor": 16777215,
    },
}


# ── Dados ─────────────────────────────────────────────────────────────────────


def _existe(pid: str) -> bool:
    return catalogo.produto(pid) is not None or len(catalogo.dados_brutos(pid)) > 1


def _canal_da_vitrine(vitrine) -> int:
    """Canal salvo para a vitrine (banco ou config). Igual ao que o JSON guarda em canal_id."""
    valor = store.ajuste(f"{str(vitrine).lower()}_channel_id")
    return int(valor) if valor and valor.isdigit() else 0


def _documento(pid: str) -> dict:
    return catalogo.dados_brutos(pid)


def _ler(texto: str) -> dict:
    try:
        dados = json.loads(texto)
    except ValueError as exc:
        raise ValueError(f"JSON inválido: {exc}") from None
    if not isinstance(dados, dict):
        raise ValueError("o JSON precisa ser um objeto, entre { e }")
    return dados


def _num(alvo: list[str], pos: int = 0) -> int:
    return int(alvo[pos]) if len(alvo) > pos and alvo[pos].isdigit() else 0


# ── Gravação ──────────────────────────────────────────────────────────────────


async def _processar(
    interaction: discord.Interaction, texto: str, pid_original: str | None
) -> None:
    """Confere forma e ID do JSON e grava. Recusas de forma respondem já; a gravação responde depois (defer)."""
    try:
        dados = _ler(texto)
    except ValueError as exc:
        await _recusar(interaction, texto, pid_original, str(exc))
        return
    pid_json = dados.get("id")
    if pid_original is not None:
        if pid_json not in (None, pid_original):
            await _recusar(
                interaction,
                texto,
                pid_original,
                f"O ID do produto não pode mudar (`{pid_original}`).",
            )
            return
        dados["id"] = pid_original
    elif pid_json and _existe(str(pid_json)):
        await _recusar(
            interaction,
            texto,
            None,
            f"Já existe um produto com o ID `{pid_json}`. Para mudar, use Editar.",
        )
        return

    await interaction.response.defer(ephemeral=True)
    try:
        rel = await produtos.cadastrar(dados, interaction.guild, interaction.user.id)
    except Exception:
        log.exception("falha ao gravar o produto %s", dados.get("id"))
        await _seguir_recusa(
            interaction,
            texto,
            pid_original,
            "Erro inesperado ao gravar. Nada foi confirmado.",
        )
        return
    if rel.erros:
        await _seguir_recusa(interaction, texto, pid_original, rel.texto())
        return
    _RASCUNHOS.pop(interaction.user.id, None)
    if rel.produto_id and not rel.sem_mudancas:
        verbo = "atualizado" if rel.atualizado else "cadastrado"
        await notify.registrar(
            "produto_alterado",
            f"{E.cardbox} Produto {verbo}",
            CORES["info"],
            [
                ("Produto", rel.produto_id),
                ("Modelo", rel.modelo_salvo or "—"),
                ("Vitrine", rel.vitrine_salva or "—"),
                ("Por", f"{interaction.user} ({interaction.user.id})"),
            ],
        )
    await seguir(
        interaction,
        painel(
            f"{E.correct} Produto",
            layout.texto(rel.texto()[:3900]),
            layout.linha(
                render.botao(
                    "Abrir o menu do produto", "ger:produto:menu", 2, "@reload"
                )
            ),
            voltar("ger:produto:menu", "Produto"),
        ),
    )


async def _recusar(
    interaction: discord.Interaction, texto: str, pid: str | None, motivo: str
) -> None:
    _RASCUNHOS[interaction.user.id] = (texto, pid)
    await rest.responder(interaction, _painel_recusa(motivo), editar=False)


async def _seguir_recusa(
    interaction: discord.Interaction, texto: str, pid: str | None, motivo: str
) -> None:
    _RASCUNHOS[interaction.user.id] = (texto, pid)
    await seguir(interaction, _painel_recusa(motivo))


def _painel_recusa(motivo: str) -> dict:
    return painel(
        f"{E.wrong} Não gravei",
        layout.texto(motivo[:3900]),
        layout.linha(
            render.botao("Reabrir o JSON", "ger:produto:rascunho", 1, "@cardbox"),
            voltar("ger:produto:menu", "Produto"),
        ),
    )


async def _reabrir(interaction: discord.Interaction) -> None:
    rascunho = _RASCUNHOS.get(interaction.user.id)
    if rascunho is None:
        await interaction.response.send_message(
            f"{E.wrong} Não há JSON para reabrir.", ephemeral=True
        )
        return
    texto, pid = rascunho
    await _abrir(
        interaction,
        f"Reabrir {pid or 'produto novo'}",
        texto,
        lambda i, t: _processar(i, t, pid),
    )


# ── Telas ─────────────────────────────────────────────────────────────────────


def menu(confirmacao: str | None = None) -> dict:
    total = len(catalogo.produtos(so_ativos=False))
    itens = []
    if confirmacao:
        itens.append(aviso(confirmacao))
    itens.append(
        layout.texto(
            "Cada produto é um JSON no mesmo formato dos arquivos de `catalogo/produtos`. "
            "Ao salvar, a vitrine é atualizada.\n"
            f"-# {total} produto(s) · [ dono ]"
        )
    )
    itens.append(
        layout.linha(
            render.botao("Adicionar", "ger:produto:add", 3, "@plus"),
            render.botao("Editar", "ger:produto:editar:0", 1, "@save"),
            render.botao("Apagar", "ger:produto:apagar:0", 4, "@delete"),
            render.botao("Listar", "ger:produto:lista:0", 2, "@cardbox"),
        )
    )
    itens.append(
        layout.linha(
            render.botao("Exemplo de JSON", "ger:produto:exemplo", 2, "@cardbox"),
            render.botao("Help", "ger:produto:help", 2, "@information"),
            render.botao("Imagem do help", "ger:produto:imagem_ajuda", 2, "@settings"),
        )
    )
    itens.append(voltar())
    return painel(f"{E.cardbox} Produto", *itens)


def _exemplo_tela() -> dict:
    """Só para copiar: o JSON completo num bloco de código. Criar e editar continuam pelos botões do menu."""
    texto = json.dumps(EXEMPLO_COMPLETO, indent=2, ensure_ascii=False)
    return painel(
        f"{E.cardbox} Exemplo de produto",
        layout.texto(
            "Copie o JSON abaixo, preencha e use **Adicionar** ou **Editar** no menu do produto."
        ),
        layout.texto(f"```json\n{texto}\n```"[:3900]),
    )


def _opcoes(lista: list) -> list[dict]:
    return [
        {
            "label": (p.nome or p.id)[:100],
            "value": p.id,
            "description": f"{p.id} · {p.vitrine} · {render.brl(p.preco)}"[:100],
        }
        for p in lista
    ]


def _selecao(
    pagina: int, cid_select: str, base: str, titulo: str, instrucao: str
) -> dict:
    todos = catalogo.produtos(so_ativos=False)
    if not todos:
        return painel(
            titulo,
            layout.texto("Nenhum produto cadastrado."),
            voltar("ger:produto:menu", "Produto"),
        )
    fatia, pagina, total = pagina_de(todos, pagina)
    itens = [
        layout.texto(instrucao + rodape_pagina(pagina, total)),
        seletor_opcoes(cid_select, "Escolha um produto...", _opcoes(fatia)),
    ]
    nav = navegacao(base, pagina, total)
    if nav:
        itens.append(nav)
    itens.append(voltar("ger:produto:menu", "Produto"))
    return painel(titulo, *itens)


def _lista(pagina: int) -> dict:
    todos = catalogo.produtos(so_ativos=False)
    fatia, pagina, total = pagina_de(todos, pagina, 10)
    corpo = "\n\n".join(
        f"**{p.nome}**\n-# `{p.id}` · {p.vitrine} · {render.brl(p.preco)} · "
        f"{'ativo' if p.ativo else 'rascunho (fora das vitrines)'}"
        for p in fatia
    )
    itens = [
        layout.texto((corpo or "Nenhum produto cadastrado.")[:3900]),
        layout.rodape(f"Página {pagina + 1} de {total} · {len(todos)} produto(s)"),
    ]
    nav = navegacao("ger:produto:lista", pagina, total)
    if nav:
        itens.append(nav)
    itens.append(voltar("ger:produto:menu", "Produto"))
    return painel(f"{E.cardbox} Produtos", *itens)


# ── Ações ─────────────────────────────────────────────────────────────────────


async def _ajuda(interaction: discord.Interaction) -> None:
    try:
        from ... import ajuda
    except ImportError:
        await rest.responder(
            interaction,
            painel(
                f"{E.information} Help do produto",
                layout.texto("A ajuda do produto ainda não está disponível."),
                voltar("ger:produto:menu", "Produto"),
            ),
            editar=False,
        )
        return
    resultado = ajuda.enviar_help_produto(interaction)
    if inspect.isawaitable(resultado):
        await resultado


async def _apagar(interaction: discord.Interaction, pid: str) -> None:
    if not _existe(pid):
        await rest.responder(
            interaction,
            menu(confirmacao=f"O produto `{pid}` já não existe."),
            editar=True,
        )
        return
    try:
        resultado = await produtos.apagar(pid, interaction.guild, interaction.user.id)
    except Exception:
        log.exception("falha ao apagar o produto %s", pid)
        await rest.responder(
            interaction,
            menu(confirmacao=f"{E.wrong} Não consegui apagar `{pid}`."),
            editar=True,
        )
        return
    erros = getattr(resultado, "erros", None) or []
    if erros:
        await rest.responder(
            interaction, menu(confirmacao=f"{E.wrong} " + "; ".join(erros)), editar=True
        )
        return
    await notify.registrar(
        "produto_alterado",
        f"{E.cardbox} Produto apagado",
        CORES["aviso"],
        [("Produto", pid), ("Por", f"{interaction.user} ({interaction.user.id})")],
    )
    await rest.responder(
        interaction, menu(confirmacao=f"Produto `{pid}` apagado."), editar=True
    )


# Ações que só o dono executa. O admin vê todos os botões; estas respondem "Só o dono".
ACOES_DONO = {
    "add",
    "editar",
    "pick_editar",
    "apagar",
    "pick_apagar",
    "apagar_ok",
    "rascunho",
    "imagem_ajuda",
}


async def tratar(interaction: discord.Interaction, acao: str, alvo: list[str]) -> None:
    if acao in ACOES_DONO and not await so_dono(interaction):
        return
    if acao in ("", "menu"):
        return await rest.responder(interaction, menu(), editar=True)
    if acao == "exemplo":
        return await rest.responder(interaction, _exemplo_tela(), editar=False)
    if acao == "add":
        return await _abrir(
            interaction,
            "Adicionar produto",
            json.dumps(MODELO_NOVO, indent=2, ensure_ascii=False),
            lambda i, t: _processar(i, t, None),
        )
    if acao == "editar":
        return await rest.responder(
            interaction,
            _selecao(
                _num(alvo),
                "ger:produto:pick_editar",
                "ger:produto:editar",
                f"{E.save} Editar produto",
                "Escolha o produto. O JSON atual abre para você mudar o que quiser.\n",
            ),
            editar=True,
        )
    if acao == "pick_editar":
        valores = interaction.data.get("values") or []
        if not valores or not _existe(valores[0]):
            return await sem_acao(interaction)
        pid = valores[0]
        return await _abrir(
            interaction,
            f"Editar {pid}",
            json.dumps(_documento(pid), indent=2, ensure_ascii=False),
            lambda i, t: _processar(i, t, pid),
        )
    if acao == "apagar":
        return await rest.responder(
            interaction,
            _selecao(
                _num(alvo),
                "ger:produto:pick_apagar",
                "ger:produto:apagar",
                f"{E.delete} Apagar produto",
                "Escolha o produto a apagar. Some o arquivo JSON, o estoque e a mensagem da vitrine dele.\n",
            ),
            editar=True,
        )
    if acao == "pick_apagar":
        valores = interaction.data.get("values") or []
        if not valores or not _existe(valores[0]):
            return await sem_acao(interaction)
        pid = valores[0]
        nome = catalogo.dados_brutos(pid).get("nome", pid)
        return await rest.responder(
            interaction,
            confirmar(
                f"Apagar {nome}",
                f"Isso apaga `{pid}` para sempre: o arquivo JSON, o estoque e a mensagem da vitrine. "
                "Não dá para desfazer.",
                f"ger:produto:apagar_ok:{pid}",
                "ger:produto:apagar:0",
                "Apagar de vez",
            ),
            editar=True,
        )
    if acao == "apagar_ok":
        return await _apagar(interaction, alvo[0] if alvo else "")
    if acao == "lista":
        return await rest.responder(interaction, _lista(_num(alvo)), editar=True)
    if acao == "rascunho":
        return await _reabrir(interaction)
    if acao == "help":
        return await _ajuda(interaction)
    if acao == "imagem_ajuda":
        from ... import ajuda

        return await interaction.response.send_modal(ajuda.ImagemAjudaModal("produto"))
    return await sem_acao(interaction)
