"""Lista fechada de ações do Ghost Agent. Nada fora daqui é alcançável pela IA.

Cada ferramenta chama a MESMA função que o comando do dono usa (produtos.cadastrar, comandos_acoes.executar_setup...).

Risco (perigo) é definido em APROVAR_ALTO, no fim deste arquivo:
- alto: pede aprovação do dono (um botão para o plano inteiro). Só ações destrutivas ou irreversíveis.
- baixo / medio: rodam direto, sem pedir aprovação.

Fora do alcance do agente (de propósito):
- credenciais, .env, tokens, IDs de canal/dono (ficam em Avançado)
- anti-raid, backup (restaurar/apagar), cargos, equipe (admins)
- reembolso, aprovação manual de pedido, ban e timeout
Ações do próprio bot não disparam o anti-raid (ele já as trata como restauração).
"""

import json
import math
import re
import unicodedata
from dataclasses import dataclass
from typing import Awaitable, Callable

import discord

from ..config import settings
from ..data import store
from . import diario

AJUDA = settings.BASE_DIR / "ghost" / "ajuda" / "help.json"
MAX_NOME = 100
DATA = re.compile(r"\d{4}-\d{2}-\d{2}")


@dataclass
class Ferramenta:
    nome: str
    descricao: str
    parametros: dict
    escrita: bool
    perigo: str  # "baixo" | "medio" | "alto"
    executar: Callable[
        [discord.Guild, dict, object], Awaitable[str]
    ]  # (servidor, args, dono)
    resumo: Callable[[dict], str]  # texto curto para o plano de confirmação
    conferir: Callable[[dict], str | None] | None = (
        None  # checagem barata antes de entrar no plano
    )

    def validar(self, args):
        if not isinstance(args, dict):
            raise ValueError("argumentos precisam ser objeto")
        schema = self.parametros
        props = schema.get("properties", {})
        if set(args) - set(props):
            raise ValueError("argumento desconhecido")
        if set(schema.get("required", [])) - set(args):
            raise ValueError("argumento obrigatório ausente")
        for name, value in args.items():
            prop = props[name]
            kind = prop.get("type")
            valid = (
                (kind == "string" and isinstance(value, str))
                or (
                    kind == "integer"
                    and isinstance(value, int)
                    and not isinstance(value, bool)
                )
                or (
                    kind == "number"
                    and isinstance(value, (int, float))
                    and not isinstance(value, bool)
                    and math.isfinite(value)
                )
            )
            if not valid or ("enum" in prop and value not in prop["enum"]):
                raise ValueError(f"argumento inválido: {name}")
            if isinstance(value, str) and len(value) > 20000:
                raise ValueError(f"argumento muito longo: {name}")
        if self.conferir:
            error = self.conferir(args)
            if error:
                raise ValueError(error)

    def declaracao(self) -> dict:
        return {
            "name": self.nome,
            "description": self.descricao,
            "parameters": self.parametros,
        }


# ── helpers ───────────────────────────────────────────────────────────────────


def _texto(args: dict, campo: str, obrigatorio: bool = True) -> str:
    valor = str(args.get(campo, "") or "").strip()
    if obrigatorio and not valor:
        raise ValueError(f"falta {campo}")
    if len(valor) > MAX_NOME:
        raise ValueError(f"{campo} passa de {MAX_NOME} caracteres")
    return valor


def _inteiro(args: dict, campo: str, minimo: int, maximo: int) -> int:
    try:
        valor = int(args.get(campo))
    except (TypeError, ValueError):
        raise ValueError(f"{campo} precisa ser um número inteiro") from None
    if not minimo <= valor <= maximo:
        raise ValueError(f"{campo} precisa estar entre {minimo} e {maximo}")
    return valor


def _nome(args: dict, campo: str = "nome") -> str:
    return str(args.get(campo, ""))[:MAX_NOME]


def _canal(guild: discord.Guild, ref: str):
    """Aceita o ID do canal (use quando houver nomes repetidos) ou o nome. Nome repetido sem ID é recusado."""
    ref = (ref or "").strip().strip("<#>")
    if ref.isdigit():
        canal = guild.get_channel_or_thread(int(ref))
        if canal is None:
            raise ValueError(f"canal com id {ref} não encontrado")
        return canal
    nome = ref
    achados = [c for c in guild.channels if c.name == nome]
    if len(achados) > 1:
        raise ValueError(
            f"há {len(achados)} canais chamados '{nome}': renomeie um antes"
        )
    if not achados:
        raise ValueError(f"canal '{nome}' não encontrado")
    return achados[0]


def _categoria(guild: discord.Guild, nome: str | None):
    if not nome:
        return None
    cat = discord.utils.get(guild.categories, name=nome)
    if cat is None:
        raise ValueError(f"categoria '{nome}' não existe")
    return cat


def _json_obj(args: dict) -> dict:
    dados = args.get("dados")
    if isinstance(dados, dict):
        return dados
    try:
        virou = json.loads(str(dados or ""))
    except ValueError:
        raise ValueError("os dados precisam ser um objeto JSON válido") from None
    if not isinstance(virou, dict):
        raise ValueError("os dados precisam ser um objeto JSON")
    return virou


def _produto_existe(pid: str) -> None:
    from .. import catalogo

    if not catalogo.existe(pid):
        raise ValueError(f"o produto '{pid}' não existe")


# ── servidor ──────────────────────────────────────────────────────────────────


async def _inspecionar(guild, args, dono) -> str:
    return json.dumps(
        {
            "nome": guild.name,
            "membros": guild.member_count,
            "canais_de_texto": [f"{c.name} (id {c.id})" for c in guild.text_channels][:200],
            "canais_de_voz": [f"{c.name} (id {c.id})" for c in guild.voice_channels][:200],
            "categorias": [f"{c.name} (id {c.id})" for c in guild.categories][:100],
            "cargos": [r.name for r in guild.roles if r != guild.default_role][:100],
        },
        ensure_ascii=False,
    )


async def _criar_categoria(guild, args, dono) -> str:
    cat = await guild.create_category(_texto(args, "nome"), reason="Ghost Agent")
    diario.informar({"acao": "apagar_canal_id", "id": cat.id})
    return f"Categoria {cat.name} criada."


async def _criar_canal(guild, args, dono) -> str:
    nome = _texto(args, "nome")
    categoria = _categoria(guild, args.get("categoria"))
    if str(args.get("tipo", "texto")) == "voz":
        canal = await guild.create_voice_channel(
            nome, category=categoria, reason="Ghost Agent"
        )
    else:
        canal = await guild.create_text_channel(
            nome, category=categoria, reason="Ghost Agent"
        )
    diario.informar({"acao": "apagar_canal_id", "id": canal.id})
    return f"Canal {canal.name} criado."


async def _renomear_canal(guild, args, dono) -> str:
    canal = _canal(guild, _texto(args, "canal"))
    antigo = canal.name
    await canal.edit(name=_texto(args, "novo_nome"), reason="Ghost Agent")
    diario.informar({"acao": "renomear", "id": canal.id, "nome": antigo})
    return f"Canal renomeado para {canal.name}."


async def _enviar_embed(guild, args, dono) -> str:
    canal = _canal(guild, _texto(args, "canal"))
    embed = discord.Embed(
        title=_texto(args, "titulo", obrigatorio=False)[:256] or None,
        description=_texto(args, "texto")[:4000],
        color=settings.CORES["info"],
    )
    await canal.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
    return f"Embed enviado em {canal.name}."


async def _apagar_canal(guild, args, dono) -> str:
    canal = _canal(guild, _texto(args, "canal"))
    nome = canal.name
    from ..services import backup

    snapshot = backup._canal_dados(canal)
    await canal.delete(reason="Ghost Agent")
    diario.informar({"acao": "recriar_canal_seguro", "snapshot": snapshot})
    return f"Canal {nome} apagado."


async def _renomear_servidor(guild, args, dono) -> str:
    antigo = guild.name
    await guild.edit(name=_texto(args, "nome"), reason="Ghost Agent")
    diario.informar({"acao": "renomear_servidor", "nome": antigo})
    return f"Servidor renomeado para {guild.name}."


async def _travar(guild, args, dono) -> str:
    from ..bot.cogs import comandos_acoes

    canal = _canal(guild, _texto(args, "canal"))
    from ..data import chats

    before = chats.pegar(canal.id)
    texto = await comandos_acoes.travar(canal, guild, dono)
    if before is None and chats.pegar(canal.id) is not None:
        diario.informar({"acao": "destravar_id", "id": canal.id})
    return texto


async def _destravar(guild, args, dono) -> str:
    from ..bot.cogs import comandos_acoes

    canal = _canal(guild, _texto(args, "canal"))
    from ..data import chats

    before = chats.pegar(canal.id)
    texto = await comandos_acoes.destravar(canal, guild, dono)
    if before is not None and chats.pegar(canal.id) is None:
        diario.informar({"acao": "travar_id", "id": canal.id})
    return texto


async def _limpar_mensagens(guild, args, dono) -> str:
    from ..bot.cogs import comandos_acoes

    quantidade = _inteiro(args, "quantidade", 1, 1000)
    texto = await comandos_acoes.limpar(
        _canal(guild, _texto(args, "canal")), quantidade
    )
    diario.informar(
        motivo="as mensagens apagadas não voltam sozinhas (só com /backup restaurar)"
    )
    return texto


# ── estilo de nome (Unicode, feito em código, não pelo modelo) ───────────────

_ESTILOS = {  # nome -> (início maiúsculas, início minúsculas) no bloco Unicode matemático
    "negrito": (0x1D400, 0x1D41A),
    "italico": (0x1D434, 0x1D44E),
    "negrito_italico": (0x1D468, 0x1D482),
}
_BURACOS_ITALICO = {
    "h": "ℎ"
}  # a itálica do 'h' cai num buraco do bloco; usa o caractere certo


def _estilizar(texto: str, estilo: str) -> str:
    if estilo == "normal":
        return texto
    if estilo not in _ESTILOS:
        raise ValueError(f"estilo inválido; use {', '.join(['normal', *_ESTILOS])}")
    maiu, minu = _ESTILOS[estilo]
    saida = []
    for ch in texto:
        if "A" <= ch <= "Z":
            saida.append(chr(maiu + ord(ch) - ord("A")))
        elif "a" <= ch <= "z":
            if estilo == "italico" and ch in _BURACOS_ITALICO:
                saida.append(_BURACOS_ITALICO[ch])
            else:
                saida.append(chr(minu + ord(ch) - ord("a")))
        else:
            saida.append(ch)
    return unicodedata.normalize("NFC", "".join(saida))


async def _estilizar_nome(guild, args, dono) -> str:
    return _estilizar(_texto(args, "texto"), _texto(args, "estilo"))


# ── imagem do /help ───────────────────────────────────────────────────────────


async def _definir_imagem_help(guild, args, dono) -> str:
    url = str(args.get("url", "")).strip()
    if url and not url.startswith("https://"):
        return "Recusado: o link precisa começar com https://."
    dados = json.loads(AJUDA.read_text(encoding="utf-8"))
    antiga = dados.get("url", "")
    dados["url"] = url
    AJUDA.write_text(
        json.dumps(dados, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    diario.informar({"acao": "imagem_help", "url": antiga})
    return "Imagem do /help atualizada." if url else "Imagem do /help removida."


# ── produtos (mesmo caminho do /produto) ─────────────────────────────────────


async def _produto_exemplo(guild, args, dono) -> str:
    from ..services import produtos

    return json.dumps(produtos.EXEMPLO, ensure_ascii=False, indent=2)[:6000]


async def _produto_listar(guild, args, dono) -> str:
    from .. import catalogo

    linhas = [
        f"{p.id} | {p.nome} | R$ {p.preco:.2f} | {'ativo' if p.ativo else 'inativo'} | vitrine {p.vitrine}"
        for p in catalogo.produtos(so_ativos=False)
    ][:50]
    return "\n".join(linhas) or "Nenhum produto cadastrado."


def _conferir_produto(args: dict) -> str | None:
    from .. import catalogo

    try:
        dados = _json_obj(args)
        pid = dados.get("id") or catalogo.gerar_id(str(dados.get("nome") or ""))
        catalogo.normalizar({**dados, "id": pid}, f"{pid}.json")
    except (ValueError, catalogo.CatalogoInvalido) as exc:
        return f"JSON recusado: {exc}"
    return None


async def _produto_salvar(guild, args, dono) -> str:
    from .. import catalogo
    from ..services import produtos

    dados = _json_obj(args)
    pid = dados.get("id") or catalogo.gerar_id(str(dados.get("nome") or ""))
    antes = catalogo.doc_produto(pid) if isinstance(pid, str) else None
    dados = {**dados, "id": pid}
    rel = await produtos.cadastrar(dados, guild, dono.id)
    if not rel.erros and not rel.sem_mudancas:
        diario.informar({"acao": "produto", "id": pid, "doc": antes})
    return rel.texto()


async def _produto_apagar(guild, args, dono) -> str:
    from .. import catalogo
    from ..services import produtos

    pid = _texto(args, "id")
    antes = catalogo.doc_produto(pid)
    rel = await produtos.apagar(pid, guild, dono.id)
    if rel.apagado:
        diario.informar(
            {
                "acao": "produto",
                "id": pid,
                "doc": antes,
                "aviso": "O estoque apagado não volta; o produto e a vitrine voltam.",
            }
        )
    return rel.texto()


# ── estoque (mesmo caminho do botão Adicionar do /gerenciar) ─────────────────


async def _estoque_ver(guild, args, dono) -> str:
    from ..data import stock

    pid = _texto(args, "produto")
    _produto_existe(pid)
    return f"{stock.count_global(pid)} item(ns) em estoque de {pid}."


async def _estoque_adicionar(guild, args, dono) -> str:
    from ..data import stock
    from ..services import avisos, shelves

    pid = _texto(args, "produto")
    _produto_existe(pid)
    linhas = [
        linha.strip()
        for linha in str(args.get("itens", "")).splitlines()
        if linha.strip()
    ]
    if not linhas:
        raise ValueError("nenhum item informado")
    antes = stock.count_global(pid)
    link = stock.is_link(pid)
    from ..database.connection import connection

    table = "estoque_links" if link else "estoque"
    with connection() as conn:
        previous = conn.execute(f"SELECT COALESCE(MAX(id), 0) FROM {table}").fetchone()[
            0
        ]
        adicionados = (
            stock.add_links(pid, linhas) if link else stock.add_accounts(pid, linhas)
        )
        ids = [
            r[0]
            for r in conn.execute(
                f"SELECT id FROM {table} WHERE id>? AND produto_key=?", (previous, pid)
            )
        ]
    if adicionados:
        diario.informar({"acao": "estoque_remover_ids", "produto": pid, "itens": ids})
    await shelves.refresh_for_product(pid)
    if adicionados and antes == 0:
        await avisos.estoque_reposto(pid, adicionados)
    ignorados = len(linhas) - adicionados
    return f"{adicionados} item(ns) adicionado(s) em {pid}." + (
        f" {ignorados} ignorado(s)." if ignorados else ""
    )


async def _estoque_limpar(guild, args, dono) -> str:
    from ..data import stock
    from ..services import shelves

    pid = _texto(args, "produto")
    _produto_existe(pid)
    itens = diario.estoque_atual(pid)
    stock.clear(pid)
    if itens:
        diario.informar({"acao": "estoque_restaurar", "produto": pid, "itens": itens})
    await shelves.refresh_for_product(pid)
    return f"Estoque de {pid} zerado."


# ── vitrines (mesmo caminho do /setup) ────────────────────────────────────────


async def _setup_vitrine(guild, args, dono) -> str:
    from ..bot.cogs import comandos_acoes

    acao = _texto(args, "acao")
    if acao not in ("add", "remove"):
        raise ValueError("ação precisa ser add ou remove")
    from ..data import store

    canal = _canal(guild, _texto(args, "canal")) if acao == "add" else None
    vitrine = comandos_acoes.vitrine_do_texto(_texto(args, "vitrine"))
    anterior = (
        store.get_config(comandos_acoes._chave_canal(vitrine.id)) if vitrine else None
    )
    linhas = await comandos_acoes.executar_setup(
        acao, _texto(args, "vitrine"), canal, None, dono
    )
    if vitrine:
        diario.informar(
            {
                "acao": "setup",
                "vitrine": vitrine.id,
                "canal_id": int(anterior) if anterior else None,
            }
        )
    return comandos_acoes.juntar(linhas)


async def _setup_atualizar(guild, args, dono) -> str:
    from ..bot.cogs import comandos_acoes

    linhas = await comandos_acoes.executar_setup(
        "atualizar", None, None, _texto(args, "ids"), dono
    )
    return comandos_acoes.juntar(linhas)


# ── cupons (mesmo caminho do /gerenciar > Cupom) ─────────────────────────────


def _conferir_cupom(args: dict) -> str | None:
    tem_pct = args.get("desconto_pct") not in (None, "")
    tem_valor = args.get("desconto_valor") not in (None, "")
    if tem_pct == tem_valor:
        return "informe desconto_pct OU desconto_valor, um só"
    if tem_pct and not 1 <= int(args["desconto_pct"]) <= 99:
        return "desconto_pct precisa estar entre 1 e 99"
    if tem_valor and float(args["desconto_valor"]) <= 0:
        return "desconto_valor precisa ser maior que zero"
    validade = str(args.get("validade") or "")
    if validade and not DATA.fullmatch(validade):
        return "validade precisa ser AAAA-MM-DD"
    return None


async def _cupom_listar(guild, args, dono) -> str:
    from ..data import coupons

    return json.dumps(coupons.listar()[:50], ensure_ascii=False, default=str)[:4000]


async def _cupom_criar(guild, args, dono) -> str:
    from ..data import coupons

    erro = _conferir_cupom(args)
    if erro:
        raise ValueError(erro)
    pct = args.get("desconto_pct")
    valor = args.get("desconto_valor")
    limite = args.get("limite_usos")
    cid = coupons.criar(
        _texto(args, "nome"),
        desconto_pct=int(pct) if pct not in (None, "") else None,
        desconto_valor=float(valor) if valor not in (None, "") else None,
        limite_usos=int(limite) if limite not in (None, "") else None,
        validade=str(args.get("validade") or "") or None,
    )
    diario.informar({"acao": "cupom_apagar", "id": cid})
    return f"Cupom {_nome(args)} criado (id {cid})."


async def _cupom_alternar(guild, args, dono) -> str:
    from ..data import coupons

    cupom = coupons.get(_texto(args, "cupom"))
    if cupom is None:
        raise ValueError("cupom não encontrado")
    ligado = coupons.alternar(cupom["id"])
    diario.informar(
        {"acao": "cupom_estado", "id": cupom["id"], "ativo": cupom["ativo"]}
    )
    return f"Cupom {cupom['nome']} {'ligado' if ligado else 'desligado'}."


# ── vendas (leitura do /dashboard) ───────────────────────────────────────────


async def _vendas(guild, args, dono) -> str:
    from ..bot.secoes import dashboard

    vendas, servidor = await dashboard.conteudo(guild)
    return f"{vendas}\n{servidor}".replace("**", "")


async def _diario_ver(guild, args, dono) -> str:
    return "\n".join(diario.rotulo(e) for e in diario.ultimas(20)) or "Diário vazio."


async def _desfazer_acao(guild, args, dono) -> str:
    return await diario.reverter(guild, int(args.get("id")), dono)


def _resumo_desfazer(args: dict) -> str:
    return f"DESFAZER a ação #{args.get('id')} do diário"


# ── análise de segurança (só leitura) ────────────────────────────────────────

PERMISSOES_PERIGOSAS = {
    "administrator": "administrador",
    "manage_guild": "gerenciar servidor",
    "manage_roles": "gerenciar cargos",
    "manage_channels": "gerenciar canais",
    "manage_webhooks": "gerenciar webhooks",
    "ban_members": "banir",
    "kick_members": "expulsar",
    "moderate_members": "silenciar",
    "mention_everyone": "mencionar @everyone",
    "manage_messages": "apagar mensagens",
    "manage_expressions": "gerenciar emojis",
    "view_audit_log": "ver log de auditoria",
}


def _perigos(permissoes) -> list[str]:
    return [
        rotulo
        for attr, rotulo in PERMISSOES_PERIGOSAS.items()
        if getattr(permissoes, attr, False)
    ]


async def _analisar_cargos(guild, args, dono) -> str:
    linhas = []
    for r in sorted(guild.roles, key=lambda r: -r.position):
        perigos = _perigos(r.permissions)
        if r.is_default() and not perigos:
            linhas.append("@everyone: sem permissões perigosas")
        elif perigos:
            linhas.append(
                f"{r.name}: {', '.join(perigos)} | {len(r.members)} membro(s)"
            )
    return "\n".join(linhas[:50]) or "Nenhum cargo com permissão perigosa."


async def _analisar_canais(guild, args, dono) -> str:
    todos = guild.default_role
    linhas = []
    for ch in guild.channels:
        if isinstance(ch, discord.CategoryChannel):
            continue
        perm = ch.permissions_for(todos)
        if not perm.view_channel:
            continue
        marcas = ["público"]
        if perm.send_messages:
            marcas.append("qualquer um escreve")
        perigos = _perigos(perm)
        if perigos:
            marcas.append("@everyone pode: " + ", ".join(perigos))
        linhas.append(f"{ch.name}: {' | '.join(marcas)}")
    return "\n".join(linhas[:50]) or "Nenhum canal visível para @everyone."


async def _analisar_membros(guild, args, dono) -> str:
    admins = [
        m for m in guild.members if not m.bot and m.guild_permissions.administrator
    ]
    bots = [m for m in guild.members if m.bot]
    linhas = [
        f"Membros: {guild.member_count} total, {len(guild.members)} em cache.",
        f"Administradores ({len(admins)}): "
        + (", ".join(f"@{m.name}" for m in admins[:30]) or "nenhum"),
        f"Bots ({len(bots)}): "
        + (
            ", ".join(
                f"@{m.name}"
                + (
                    f" [{', '.join(_perigos(m.guild_permissions))}]"
                    if _perigos(m.guild_permissions)
                    else ""
                )
                for m in bots[:30]
            )
            or "nenhum"
        ),
    ]
    return "\n".join(linhas)[:3000]


async def _analisar_convites(guild, args, dono) -> str:
    convites = await guild.invites()
    linhas = []
    for c in convites[:50]:
        validade = "sem validade" if not c.max_age else "com validade"
        limite = "sem limite de usos" if not c.max_uses else f"limite {c.max_uses}"
        quem = c.inviter.name if c.inviter else "?"
        linhas.append(
            f"{c.code} | canal {c.channel.name if c.channel else '?'} | usos {c.uses} | "
            f"{validade} | {limite} | criado por {quem}"
        )
    return "\n".join(linhas) or "Nenhum convite ativo."


async def _analisar_webhooks(guild, args, dono) -> str:
    hooks = await guild.webhooks()
    linhas = [
        f"{h.name} | canal {h.channel.name if h.channel else '?'} | criado por "
        f"{h.user.name if h.user else '?'}"
        for h in hooks[:50]
    ]
    return "\n".join(linhas) or "Nenhum webhook."


# ── resumos para o plano ──────────────────────────────────────────────────────


def _resumo_produto(args: dict) -> str:
    try:
        nome = _json_obj(args).get("nome") or _json_obj(args).get("id")
    except ValueError:
        nome = "?"
    return f"salvar produto {nome}"


FERRAMENTAS: dict[str, Ferramenta] = {
    f.nome: f
    for f in [
        # servidor
        Ferramenta(
            "servidor_inspecionar",
            "Mostra nome, membros, canais, categorias e cargos do servidor.",
            {"type": "object", "properties": {}},
            False,
            "baixo",
            _inspecionar,
            lambda a: "ler o servidor",
        ),
        Ferramenta(
            "criar_categoria",
            "Cria uma categoria de canais.",
            {
                "type": "object",
                "properties": {"nome": {"type": "string"}},
                "required": ["nome"],
            },
            True,
            "medio",
            _criar_categoria,
            lambda a: f"criar categoria {_nome(a)}",
        ),
        Ferramenta(
            "criar_canal",
            "Cria um canal de texto ou de voz, opcionalmente dentro de uma categoria existente.",
            {
                "type": "object",
                "properties": {
                    "nome": {"type": "string"},
                    "categoria": {"type": "string"},
                    "tipo": {"type": "string", "enum": ["texto", "voz"]},
                },
                "required": ["nome"],
            },
            True,
            "medio",
            _criar_canal,
            lambda a: (
                f"criar canal {_nome(a)}"
                + (f" em {a['categoria']}" if a.get("categoria") else "")
            ),
        ),
        Ferramenta(
            "enviar_embed",
            "Envia um embed (caixa com título e texto) num canal. Use para aviso ou comunicado longo.",
            {
                "type": "object",
                "properties": {
                    "canal": {"type": "string", "description": "nome ou id do canal"},
                    "titulo": {"type": "string"},
                    "texto": {"type": "string"},
                },
                "required": ["canal", "texto"],
            },
            True,
            "baixo",
            _enviar_embed,
            lambda a: f"enviar embed em {a.get('canal', '')}",
        ),
        Ferramenta(
            "renomear_canal",
            "Renomeia um canal existente.",
            {
                "type": "object",
                "properties": {
                    "canal": {"type": "string"},
                    "novo_nome": {"type": "string"},
                },
                "required": ["canal", "novo_nome"],
            },
            True,
            "medio",
            _renomear_canal,
            lambda a: f"renomear {_nome(a, 'canal')} para {_nome(a, 'novo_nome')}",
        ),
        Ferramenta(
            "renomear_servidor",
            "Muda o nome do servidor.",
            {
                "type": "object",
                "properties": {"nome": {"type": "string"}},
                "required": ["nome"],
            },
            True,
            "medio",
            _renomear_servidor,
            lambda a: f"renomear o servidor para {_nome(a)}",
        ),
        Ferramenta(
            "apagar_canal",
            "Apaga um canal existente. Ação perigosa: sempre entra na confirmação. Use o id do canal quando houver nomes repetidos.",
            {
                "type": "object",
                "properties": {"canal": {"type": "string", "description": "nome ou id do canal"}},
                "required": ["canal"],
            },
            True,
            "alto",
            _apagar_canal,
            lambda a: f"APAGAR o canal {_nome(a, 'canal')}",
        ),
        Ferramenta(
            "travar_canal",
            "Trava um canal: ninguém fala até destravar (igual ao /lock).",
            {
                "type": "object",
                "properties": {"canal": {"type": "string"}},
                "required": ["canal"],
            },
            True,
            "medio",
            _travar,
            lambda a: f"travar {_nome(a, 'canal')}",
        ),
        Ferramenta(
            "destravar_canal",
            "Destrava um canal travado (igual ao /unlock).",
            {
                "type": "object",
                "properties": {"canal": {"type": "string"}},
                "required": ["canal"],
            },
            True,
            "medio",
            _destravar,
            lambda a: f"destravar {_nome(a, 'canal')}",
        ),
        Ferramenta(
            "limpar_mensagens",
            "Apaga de 1 a 1000 mensagens recentes de um canal (igual ao /clear). Ação perigosa.",
            {
                "type": "object",
                "properties": {
                    "canal": {"type": "string"},
                    "quantidade": {"type": "integer"},
                },
                "required": ["canal", "quantidade"],
            },
            True,
            "alto",
            _limpar_mensagens,
            lambda a: (
                f"APAGAR até {a.get('quantidade')} mensagens em {_nome(a, 'canal')}"
            ),
        ),
        Ferramenta(
            "estilizar_nome",
            "Converte um texto para negrito-itálico, negrito, itálico ou normal. Use isso para nomes estilizados "
            "de canal: NUNCA escreva os caracteres estilizados à mão.",
            {
                "type": "object",
                "properties": {
                    "texto": {"type": "string"},
                    "estilo": {
                        "type": "string",
                        "enum": ["normal", "negrito", "italico", "negrito_italico"],
                    },
                },
                "required": ["texto", "estilo"],
            },
            False,
            "baixo",
            _estilizar_nome,
            lambda a: "estilizar nome",
        ),
        # /help
        Ferramenta(
            "ajuda_definir_imagem",
            "Troca a imagem grande do /help. Use url vazia para tirar.",
            {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "link https da imagem"}
                },
                "required": ["url"],
            },
            True,
            "medio",
            _definir_imagem_help,
            lambda a: f"imagem do /help: {a.get('url') or '(remover)'}",
        ),
        # produtos
        Ferramenta(
            "produto_exemplo",
            "Devolve o JSON completo de exemplo de um produto, com todos os campos. "
            "Leia antes de criar um produto.",
            {"type": "object", "properties": {}},
            False,
            "baixo",
            _produto_exemplo,
            lambda a: "ler exemplo",
        ),
        Ferramenta(
            "produto_listar",
            "Lista os produtos cadastrados (id, nome, preço, ativo, vitrine).",
            {"type": "object", "properties": {}},
            False,
            "baixo",
            _produto_listar,
            lambda a: "listar produtos",
        ),
        Ferramenta(
            "produto_salvar",
            "Cria ou atualiza um produto. 'dados' é o JSON do produto, no formato de produto_exemplo. "
            "Se o id já existir, atualiza.",
            {
                "type": "object",
                "properties": {
                    "dados": {"type": "string", "description": "JSON do produto"}
                },
                "required": ["dados"],
            },
            True,
            "medio",
            _produto_salvar,
            _resumo_produto,
            _conferir_produto,
        ),
        Ferramenta(
            "produto_apagar",
            "Apaga um produto, o estoque dele e a parte dele na vitrine. Ação perigosa.",
            {
                "type": "object",
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
            },
            True,
            "alto",
            _produto_apagar,
            lambda a: f"APAGAR o produto {_nome(a, 'id')}",
        ),
        # estoque
        Ferramenta(
            "estoque_ver",
            "Mostra quantos itens tem no estoque de um produto.",
            {
                "type": "object",
                "properties": {"produto": {"type": "string"}},
                "required": ["produto"],
            },
            False,
            "baixo",
            _estoque_ver,
            lambda a: "ver estoque",
        ),
        Ferramenta(
            "estoque_adicionar",
            "Adiciona itens ao estoque. 'itens' tem um item por linha: contas em login:senha, links em https.",
            {
                "type": "object",
                "properties": {
                    "produto": {"type": "string"},
                    "itens": {"type": "string"},
                },
                "required": ["produto", "itens"],
            },
            True,
            "medio",
            _estoque_adicionar,
            lambda a: f"adicionar estoque em {_nome(a, 'produto')}",
        ),
        Ferramenta(
            "estoque_limpar",
            "Zera todo o estoque de um produto. Ação perigosa.",
            {
                "type": "object",
                "properties": {"produto": {"type": "string"}},
                "required": ["produto"],
            },
            True,
            "alto",
            _estoque_limpar,
            lambda a: f"ZERAR o estoque de {_nome(a, 'produto')}",
        ),
        # vitrines
        Ferramenta(
            "setup_vitrine",
            "Põe uma vitrine num canal (add, com o nome do canal) ou tira a vitrine (remove). Igual ao /setup.",
            {
                "type": "object",
                "properties": {
                    "acao": {"type": "string", "enum": ["add", "remove"]},
                    "vitrine": {"type": "string"},
                    "canal": {"type": "string"},
                },
                "required": ["acao", "vitrine"],
            },
            True,
            "medio",
            _setup_vitrine,
            lambda a: (
                f"{'pôr' if a.get('acao') == 'add' else 'tirar'} a vitrine {_nome(a, 'vitrine')}"
                + (f" no canal {_nome(a, 'canal')}" if a.get("acao") == "add" else "")
            ),
        ),
        Ferramenta(
            "setup_atualizar",
            "Atualiza as vitrines publicadas. 'ids' é 'todos' ou IDs separados por vírgula.",
            {
                "type": "object",
                "properties": {"ids": {"type": "string"}},
                "required": ["ids"],
            },
            True,
            "medio",
            _setup_atualizar,
            lambda a: f"atualizar vitrines ({_nome(a, 'ids')})",
        ),
        # cupons
        Ferramenta(
            "cupom_listar",
            "Lista os cupons.",
            {"type": "object", "properties": {}},
            False,
            "baixo",
            _cupom_listar,
            lambda a: "listar cupons",
        ),
        Ferramenta(
            "cupom_criar",
            "Cria um cupom. Use desconto_pct (1 a 99) OU desconto_valor (em reais), nunca os dois. "
            "validade em AAAA-MM-DD, opcional.",
            {
                "type": "object",
                "properties": {
                    "nome": {"type": "string"},
                    "desconto_pct": {"type": "integer"},
                    "desconto_valor": {"type": "number"},
                    "limite_usos": {"type": "integer"},
                    "validade": {"type": "string"},
                },
                "required": ["nome"],
            },
            True,
            "medio",
            _cupom_criar,
            lambda a: f"criar cupom {_nome(a)}",
            _conferir_cupom,
        ),
        Ferramenta(
            "cupom_alternar",
            "Liga ou desliga um cupom pelo nome.",
            {
                "type": "object",
                "properties": {"cupom": {"type": "string"}},
                "required": ["cupom"],
            },
            True,
            "medio",
            _cupom_alternar,
            lambda a: f"ligar/desligar cupom {_nome(a, 'cupom')}",
        ),
        # análise de segurança (só leitura)
        Ferramenta(
            "analisar_cargos",
            "Lista os cargos com permissões perigosas e quantos membros têm cada um.",
            {"type": "object", "properties": {}},
            False,
            "baixo",
            _analisar_cargos,
            lambda a: "analisar cargos",
        ),
        Ferramenta(
            "analisar_canais",
            "Mostra quais canais @everyone vê e escreve, e o que mais ele pode fazer.",
            {"type": "object", "properties": {}},
            False,
            "baixo",
            _analisar_canais,
            lambda a: "analisar canais",
        ),
        Ferramenta(
            "analisar_membros",
            "Mostra administradores, bots e as permissões perigosas dos bots.",
            {"type": "object", "properties": {}},
            False,
            "baixo",
            _analisar_membros,
            lambda a: "analisar membros",
        ),
        Ferramenta(
            "analisar_convites",
            "Lista os convites ativos: quem criou, usos, validade e limite.",
            {"type": "object", "properties": {}},
            False,
            "baixo",
            _analisar_convites,
            lambda a: "analisar convites",
        ),
        Ferramenta(
            "analisar_webhooks",
            "Lista os webhooks do servidor e quem os criou.",
            {"type": "object", "properties": {}},
            False,
            "baixo",
            _analisar_webhooks,
            lambda a: "analisar webhooks",
        ),
        # diário: desfazer só entra num plano, e o dono aprova
        Ferramenta(
            "diario_ver",
            "Lista as últimas ações feitas pelo agente, com número, status e se dá pra desfazer.",
            {"type": "object", "properties": {}},
            False,
            "baixo",
            _diario_ver,
            lambda a: "ver diário",
        ),
        Ferramenta(
            "desfazer_acao",
            "Desfaz uma ação anterior do diário pelo número dela. Só para quando o dono pedir.",
            {
                "type": "object",
                "properties": {"id": {"type": "integer"}},
                "required": ["id"],
            },
            True,
            "medio",
            _desfazer_acao,
            _resumo_desfazer,
            diario.conferir_desfazer,
        ),
        # vendas
        Ferramenta(
            "vendas_ver",
            "Mostra o resumo de vendas, estoque e membros (igual ao /dashboard).",
            {"type": "object", "properties": {}},
            False,
            "baixo",
            _vendas,
            lambda a: "ver vendas",
        ),
    ]
}


# Só o que apaga ou zera coisas pede aprovação do dono. O resto (criar canal, apagar mensagem, editar produto...)
# roda direto, sem plano. Mudar esta lista é a única forma de mudar o que pede aprovação.
APROVAR_ALTO = {"apagar_canal", "produto_apagar", "estoque_limpar", "desfazer_acao"}
for _ferramenta_nome, _ferramenta in FERRAMENTAS.items():  # sem sobrescrever o helper _nome()
    _ferramenta.perigo = "alto" if _ferramenta_nome in APROVAR_ALTO else "baixo"

# Política por ferramenta, escolhida pelo dono (/agente permissao): permitir roda direto; perguntar entra no plano
# e pede aprovação; negar bloqueia. Sem escolha, vale o padrão: perguntar nas destrutivas, permitir no resto.
POLITICAS = ("permitir", "perguntar", "negar")
CHAVE_POLITICA = "agente_politica"


def politicas_salvas() -> dict[str, str]:
    try:
        dados = json.loads(store.get_config(CHAVE_POLITICA) or "{}")
    except (ValueError, TypeError):
        return {}
    if not isinstance(dados, dict):
        return {}
    return {k: v for k, v in dados.items() if k in FERRAMENTAS and v in POLITICAS}


def decisao(nome: str) -> str:
    salva = politicas_salvas().get(nome)
    if salva:
        return salva
    return "perguntar" if nome in APROVAR_ALTO else "permitir"


def politicas_atuais() -> dict[str, str]:
    return {nome: decisao(nome) for nome in FERRAMENTAS}


def definir_politica(nome: str, politica: str) -> None:
    if nome not in FERRAMENTAS or politica not in POLITICAS:
        raise ValueError("ferramenta ou política desconhecida")
    atual = politicas_salvas()
    atual[nome] = politica
    store.set_config(CHAVE_POLITICA, json.dumps(atual, ensure_ascii=False))


def declaracoes() -> list[dict]:
    return [f.declaracao() for f in FERRAMENTAS.values()]
