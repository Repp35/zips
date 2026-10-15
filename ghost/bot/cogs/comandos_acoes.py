"""Ações dos comandos de barra e de prefixo (>). A mesma regra serve aos dois jeitos de chamar.

Cada função devolve o texto da resposta e não envia nada; quem chama decide como responder (interação,
mensagem ou mensagem efêmera). Falhas esperadas viram texto. O que sobra sobe para quem chamou.
"""

import io
import logging
import re

import discord
from discord.ext import commands

from ... import catalogo
from ...catalogo import render
from ...data import coupons, orders, store
from ...database.connection import connection
from ...ui.emojis import PREFIXO, E
from ...services import carts, mercadopago, notify, payments, rest, rotina, shelves
from ...config.settings import CORES

log = logging.getLogger("comandos")

EMOJI_COLADO = re.compile(r"<(a?):(\w{2,32}):(\d{15,25})>")
EMOJI_NOME = re.compile(r"[A-Za-z0-9_]{2,32}")
MAX_COMPRAS = 20
TETO_TEXTO = 1800


# ── Permissão dos comandos de prefixo ─────────────────────────────────────────


def pode(user_id: int, nivel: str) -> bool:
    """Dois níveis: dono e admin. 'staff' é o mesmo que admin."""
    if nivel == "staff":
        return store.is_admin(user_id) or store.is_staff(user_id)
    return store.is_admin(user_id)


def exige(nivel: str):
    """Check de prefixo: confere o nível e responde curto quando não pode."""

    async def predicado(ctx: commands.Context) -> bool:
        if pode(ctx.author.id, nivel):
            return True
        await ctx.reply(f"{E.wrong} Sem permissão.", mention_author=False)
        return False

    return commands.check(predicado)


def juntar(linhas: list[str]) -> str:
    """Une as linhas de resposta sem passar do limite de uma mensagem comum."""
    texto = ""
    for i, linha in enumerate(linhas):
        if len(texto) + len(linha) + 1 > TETO_TEXTO:
            return f"{texto}\n-# ... e mais {len(linhas) - i} linha(s)."
        texto = f"{texto}\n{linha}" if texto else linha
    return texto or f"{E.wrong} Nada a fazer."


# ── Emojis ────────────────────────────────────────────────────────────────────


def _colado(texto: str) -> tuple[int, str, bool] | None:
    m = EMOJI_COLADO.fullmatch(texto.strip())
    return None if m is None else (int(m.group(3)), m.group(2), m.group(1) == "a")


def achar_emoji(
    guild: discord.Guild | None, texto: str
) -> tuple[int, str | None, bool] | None:
    """Emoji colado (<:nome:id>), ID ou nome. Devolve (id, nome, animado) ou None quando não acha."""
    texto = (texto or "").strip()
    colado = _colado(texto)
    if colado is not None:
        return colado
    if texto.isdigit():
        emoji = guild.get_emoji(int(texto)) if guild else None
        return (
            (int(texto), emoji.name, emoji.animated)
            if emoji
            else (int(texto), None, False)
        )
    emoji = discord.utils.get(guild.emojis, name=texto) if guild else None
    return (emoji.id, emoji.name, emoji.animated) if emoji else None


async def inspecionar(
    guild: discord.Guild | None,
    eid: int,
    nome: str | None = None,
    animado: bool = False,
) -> tuple[str, discord.File | None]:
    """Ficha do emoji e a imagem anexada. Emoji de outro servidor não mostra quem criou."""
    emoji = guild.get_emoji(eid) if guild else None
    if emoji is not None:
        nome, animado = emoji.name, emoji.animated
        autor = str(emoji.user) if emoji.user else "não disponível"
        criado = emoji.created_at
        url = str(emoji.url)
    else:
        autor = "não disponível (emoji de outro servidor)"
        criado = discord.utils.snowflake_time(eid)
        url = f"https://cdn.discordapp.com/emojis/{eid}.{'gif' if animado else 'png'}"
    linhas = [
        f"**Nome:** `{nome or 'desconhecido'}`",
        f"**ID:** `{eid}`",
        f"**Animado:** {'sim' if animado else 'não'}",
        f"**Criado por:** {autor}",
        f"**Criado em:** <t:{int(criado.timestamp())}:f>",
        f"**Link:** {url}",
        f"**Uso:** `<{'a' if animado else ''}:{nome or 'nome'}:{eid}>`",
    ]
    try:
        dados, tipo = await rest.baixar_imagem(url)
    except (
        Exception
    ) as exc:  # RuntimeError do rest ou erro de rede: a ficha segue sem imagem
        log.info("imagem do emoji %s indisponível: %s", eid, exc)
        return "\n".join(linhas) + f"\n-# Não consegui anexar a imagem: {exc}.", None
    extensao = tipo.split("/")[1]
    return "\n".join(linhas), discord.File(
        io.BytesIO(dados), filename=f"{nome or eid}.{extensao}"
    )


async def adicionar_emoji(
    guild: discord.Guild | None,
    autor,
    nome: str | None,
    url: str | None,
    anexo: discord.Attachment | None,
) -> str:
    if guild is None:
        return f"{E.wrong} Use dentro do servidor."
    nome = (nome or "").strip()
    if not EMOJI_NOME.fullmatch(nome):
        return f"{E.wrong} Nome inválido: use de 2 a 32 letras, números ou `_`."
    if discord.utils.get(guild.emojis, name=nome):
        return f"{E.wrong} Já existe um emoji `{nome}` neste servidor."
    if bool(url) == bool(anexo):
        return f"{E.wrong} Informe um link https ou anexe uma imagem (um ou outro)."
    try:
        if url:
            if not url.strip().startswith("https://"):
                return f"{E.wrong} O link precisa começar com https://."
            dados, _tipo = await rest.baixar_imagem(url.strip())
        else:
            if anexo.content_type not in rest.TIPOS_IMAGEM:
                return f"{E.wrong} O anexo não é PNG, JPG, GIF ou WEBP."
            if anexo.size > rest.LIMITE_EMOJI_BYTES:
                return f"{E.wrong} Imagem maior que 256 KB."
            dados = await anexo.read()
    except Exception as exc:
        log.warning("imagem do emoji não baixada: %s", exc)
        return f"{E.wrong} Não consegui usar a imagem: {exc}."
    try:
        emoji = await guild.create_custom_emoji(
            name=nome, image=dados, reason=f"Adicionado por {autor}"
        )
    except discord.HTTPException as exc:
        return f"{E.wrong} O Discord recusou o emoji: {exc}"
    await notify.registrar(
        "config_alterada",
        f"{E.plus} Emoji adicionado",
        CORES["info"],
        [("Emoji", nome), ("ID", str(emoji.id)), ("Por", f"{autor} ({autor.id})")],
    )
    return f"{E.correct} Emoji {emoji} criado como `{nome}`."


async def remover_emoji(guild: discord.Guild | None, autor, alvo: str) -> str:
    if guild is None:
        return f"{E.wrong} Use dentro do servidor."
    alvo = (alvo or "").strip()
    if not alvo:
        return f"{E.wrong} Informe o nome ou o ID do emoji."
    colado = _colado(alvo)
    if colado is not None:
        emoji = guild.get_emoji(colado[0])
    elif alvo.isdigit():
        emoji = guild.get_emoji(int(alvo))
    else:
        emoji = discord.utils.get(guild.emojis, name=alvo.strip(":"))
    if emoji is None:
        return f"{E.wrong} Não achei o emoji `{alvo}` neste servidor."
    if emoji.name.startswith(PREFIXO):
        return f"{E.alert} `{emoji.name}` é emoji visual do bot e não pode ser removido por aqui."
    nome, eid = emoji.name, emoji.id
    try:
        await emoji.delete(reason=f"Removido por {autor}")
    except discord.HTTPException as exc:
        return f"{E.wrong} O Discord recusou a remoção: {exc}"
    await notify.registrar(
        "config_alterada",
        f"{E.delete} Emoji removido",
        CORES["aviso"],
        [("Emoji", nome), ("ID", str(eid)), ("Por", f"{autor} ({autor.id})")],
    )
    return f"{E.correct} Emoji `{nome}` removido."


# ── Setup de vitrines ─────────────────────────────────────────────────────────


def vitrine_do_texto(texto: str | None) -> catalogo.Vitrine | None:
    alvo = (texto or "").strip().lower()
    for v in catalogo.vitrines():
        if alvo and alvo in (v.id.lower(), v.nome_setup.lower()):
            return v
    return None


def _chave_canal(vitrine_id: str) -> str:
    return f"{vitrine_id.lower()}_channel_id"


async def _setup_add(v: catalogo.Vitrine, canal, autor) -> str:
    store.set_config(_chave_canal(v.id), str(canal.id))
    if not v.publicar:
        return f"{E.alert} `{v.nome_setup}` não é pública. O canal {canal.mention} foi salvo, mas nada foi publicado."
    await shelves.publish(v.id, fallback_channel=canal)
    salva = store.setup_message(v.id)
    await notify.registrar(
        "vitrine_publicada",
        f"{E.cart} Vitrine publicada",
        CORES["info"],
        [
            ("Vitrine", v.id),
            ("Canal", str(salva["channel_id"])),
            ("Por", f"{autor} ({autor.id})"),
        ],
    )
    return f"{E.correct} `{v.nome_setup}` publicada em <#{salva['channel_id']}>."


async def _setup_remove(v: catalogo.Vitrine, autor) -> str:
    await shelves.retirar(v.id)
    store.reset_config(_chave_canal(v.id))
    await notify.registrar(
        "config_alterada",
        f"{E.delete} Vitrine retirada",
        CORES["aviso"],
        [("Vitrine", v.id), ("Por", f"{autor} ({autor.id})")],
    )
    return f"{E.correct} `{v.nome_setup}` retirada: mensagem apagada e canal salvo removido."


async def _setup_atualizar(ids: str) -> list[str]:
    texto = (ids or "").strip()
    if texto.lower() == "todos":
        pedidos = [p.id for p in catalogo.produtos(so_ativos=False)]
    else:
        pedidos = [x.strip() for x in texto.split(",") if x.strip()]
    if not pedidos:
        return [f"{E.wrong} Informe IDs separados por vírgula, ou `todos`."]
    linhas = []
    for pid in dict.fromkeys(pedidos):
        produto = catalogo.produto(pid)
        if produto is None:
            linhas.append(f"{E.wrong} `{pid}`: produto não encontrado.")
        elif store.setup_message(produto.vitrine) is None:
            linhas.append(
                f"{E.alert} `{pid}`: a vitrine `{produto.vitrine}` não está publicada. Use `add` antes."
            )
        else:
            await shelves.refresh_for_product(pid)
            linhas.append(
                f"{E.correct} `{pid}`: atualização enviada à vitrine `{produto.vitrine}`."
            )
    return linhas


async def executar_setup(
    acao: str, vitrine_texto: str | None, canal, ids: str | None, autor
) -> list[str]:
    """acao: add, remove ou atualizar. `canal` vale só para add."""
    try:
        if acao == "atualizar":
            return await _setup_atualizar(ids or "")
        v = vitrine_do_texto(vitrine_texto)
        if v is None:
            opcoes = ", ".join(f"`{x.id}`" for x in catalogo.vitrines())
            return [f"{E.wrong} Escolha uma vitrine: {opcoes}."]
        if acao == "add":
            if canal is None:
                return [f"{E.wrong} Informe o canal da vitrine."]
            return [await _setup_add(v, canal, autor)]
        if acao == "remove":
            return [await _setup_remove(v, autor)]
        return [f"{E.wrong} Ação inválida."]
    except (RuntimeError, discord.HTTPException) as exc:
        log.exception("falha no setup (%s)", acao)
        return [f"{E.wrong} Não foi possível concluir: {exc}"]


# ── Pedidos ───────────────────────────────────────────────────────────────────


def achar_produto(texto: str) -> catalogo.Produto | None:
    """ID do produto ou nome exato (sem diferenciar maiúsculas)."""
    texto = texto.strip()
    produto = catalogo.produto(texto)
    if produto is not None:
        return produto
    return next(
        (
            p
            for p in catalogo.produtos(so_ativos=False)
            if p.nome.lower() == texto.lower()
        ),
        None,
    )


async def aprovar(
    canal, autor, guild, usuario, produto: str | None, quantidade: int, gratis: bool
) -> str:
    """Sem usuário e produto: aprova o pedido pendente da thread. Com eles: entrega manual."""
    if usuario is None and produto is None:
        if not isinstance(canal, discord.Thread):
            return f"{E.wrong} Use dentro da thread do pedido, ou informe usuário e produto."
        pendentes = orders.pending_in_thread(canal.id)
        if not pendentes:
            return f"{E.wrong} Não há pedido pendente nesta thread."
        pedido_id = pendentes[0]["id"]
        motivo = await payments.aprovar_manual(canal, pedido_id, autor)
        return (
            f"{E.wrong} {motivo}"
            if motivo
            else f"{E.correct} Pedido `#{pedido_id}` aprovado."
        )
    if usuario is None or produto is None:
        return f"{E.wrong} Para entregar manualmente, informe usuário e produto juntos."
    if not 1 <= quantidade <= 10:
        return f"{E.wrong} A quantidade vai de 1 a 10."
    p = achar_produto(produto)
    if p is None:
        return f"{E.wrong} Produto `{produto}` não encontrado."
    if p.modelo.entrega != "automatica":
        return f"{E.wrong} Este produto não tem entrega automática."
    valor = 0.0 if gratis else p.preco
    entregues, motivo = await rotina.entregar_itens(
        p.id, usuario, quantidade, valor, guild, autor.id
    )
    await notify.registrar(
        "entrega_manual_concluida",
        f"{E.truck} Entrega manual",
        CORES["info"],
        [
            ("Produto", p.nome),
            ("Cliente", f"{usuario} ({usuario.id})"),
            ("Entregues", f"{entregues} de {quantidade}"),
            ("Grátis", "sim" if gratis else "não"),
            ("Por", f"{autor} ({autor.id})"),
        ],
    )
    if motivo is None:
        return f"{E.correct} {entregues} item(ns) de `{p.nome}` enviado(s) na DM de {usuario.mention}."
    if entregues:
        return (
            f"{E.alert} Entregues {entregues} de {quantidade}: {motivo}. "
            "Os itens restantes voltaram ao estoque."
        )
    return f"{E.wrong} Nada foi entregue: {motivo}."


async def fechar(canal, autor) -> str:
    """Fecha o pedido pendente da thread e apaga a thread (mesma lógica do antigo /fechar)."""
    if not isinstance(canal, discord.Thread):
        return f"{E.wrong} Use este comando dentro da thread do pedido."
    tid = str(canal.id)
    fechados = 0
    for pedido in orders.pending_in_thread(tid):
        if pedido["payment_id"]:
            try:
                if not await mercadopago.cancel_payment(pedido["payment_id"]):
                    return f"{E.wrong} Não fechei: o PIX não foi cancelado no Mercado Pago."
            except mercadopago.PixError as exc:
                log.warning(
                    "não foi possível cancelar o PIX %s: %s", pedido["payment_id"], exc
                )
        if not pedido["payment_id"]:
            return f"{E.alert} PIX com resultado incerto; retome a criação antes de cancelar."
        if not orders.set_status(pedido["id"], "cancelled"):
            return f"{E.alert} O pedido mudou durante o cancelamento; confira antes de fechar."
        coupons.refund_for_pedido(pedido["id"])
        fechados += 1
        await notify.registrar(
            "pix_encerrado",
            f"{E.wrong} Pedido fechado pelo admin",
            CORES["aviso"],
            [
                ("Pedido", f"#{pedido['id']}"),
                ("Fechado por", f"{autor} ({autor.id})"),
                ("Thread", canal.name),
            ],
        )
    store.close_restock_thread(tid)
    await carts.close(tid)
    return f"{E.correct} Thread fechada ({fechados} pedido(s) cancelado(s))."


# ── Perfil de cliente ─────────────────────────────────────────────────────────


def _compras(user_id: int):
    with connection() as conn:
        return conn.execute(
            "SELECT id, produto_key, valor, criado_em FROM pedidos WHERE discord_user_id=? AND status='approved' "
            "ORDER BY id DESC LIMIT ?",
            (str(user_id), MAX_COMPRAS),
        ).fetchall()


def _data(criado_em: str) -> str:
    dia = (criado_em or "")[:10]
    return "/".join(reversed(dia.split("-"))) if dia.count("-") == 2 else dia


def perfil(usuario) -> tuple[str, str]:
    """Sem usuário: ranking dos 10 que mais compraram. Com usuário: resumo e lista de compras."""
    if usuario is None:
        linhas = [
            f"**{i}.** <@{r['discord_user_id']}> · {render.brl(r['total'])} · {r['n']} pedido(s)"
            for i, r in enumerate(orders.top_clientes(10), start=1)
        ]
        return f"{E.members} Ranking de clientes", "\n".join(
            linhas
        ) or "Nenhuma compra aprovada ainda."
    n, total = orders.resumo_cliente(usuario.id)
    compras = _compras(usuario.id)
    linhas = [
        f"`#{r['id']}` · `{store.produto_nome(r['produto_key'])}` · {render.brl(r['valor'])} · "
        f"{_data(r['criado_em'])}"
        for r in compras
    ]
    banido = " · **banido**" if store.is_banned(usuario.id) else ""
    cabeca = (
        f"<@{usuario.id}>{banido}\n**Pedidos aprovados:** {n}\n"
        f"**Total gasto:** {render.brl(total)}\n\n**Compras:**\n"
    )
    corpo = "\n".join(linhas) or "Nenhuma compra ainda."
    if n > len(compras):
        corpo += f"\n-# Mostrando as {len(compras)} mais recentes de {n}."
    return f"{E.member} Perfil", cabeca + corpo


# ── Limpeza e canais ──────────────────────────────────────────────────────────

MAX_LIMPEZA = 1000

async def limpar(canal, quantidade: int, antes=None) -> str:
    if not 1 <= quantidade <= MAX_LIMPEZA:
        return f"{E.wrong} A quantidade vai de 1 a {MAX_LIMPEZA}."
    if not hasattr(canal, "purge"):
        return f"{E.wrong} Este canal não permite limpeza."
    try:
        apagadas = await canal.purge(limit=quantidade, before=antes)
    except discord.HTTPException as exc:
        return f"{E.wrong} Não consegui apagar: {exc}"
    return f"{E.correct} {len(apagadas)} mensagem(ns) apagada(s)."


async def travar(canal, guild, autor) -> str:
    """/lock: ninguém fala no chat até o dono usar /unlock. Guarda a permissão original de @everyone."""
    from datetime import datetime
    from ...data import chats

    if isinstance(canal, discord.Thread):
        if canal.locked:
            return f"{E.information} Esta thread já está travada."
        try:
            await canal.edit(locked=True)
        except discord.HTTPException as exc:
            return f"{E.wrong} Não consegui travar: {exc}"
        chats.guardar(
            canal.id,
            False,
            0,
            0,
            f"{autor} ({autor.id})",
            datetime.now().isoformat(timespec="seconds"),
        )
    elif guild is not None and isinstance(canal, discord.TextChannel):
        if chats.pegar(canal.id) is not None:
            return f"{E.information} Este chat já está travado."
        padrao = guild.default_role
        atual = canal.overwrites.get(padrao)
        permite, nega = (
            atual.pair()
            if atual is not None
            else (discord.Permissions(0), discord.Permissions(0))
        )
        chats.guardar(
            canal.id,
            atual is not None,
            permite.value,
            nega.value,
            f"{autor} ({autor.id})",
            datetime.now().isoformat(timespec="seconds"),
        )
        novo = atual or discord.PermissionOverwrite()
        novo.send_messages = False
        try:
            await canal.set_permissions(padrao, overwrite=novo, reason="Chat travado")
        except discord.HTTPException as exc:
            chats.apagar(canal.id)
            return f"{E.wrong} Não consegui travar: {exc}"
    else:
        return f"{E.wrong} Só dá para travar canais de texto e threads."
    await notify.registrar(
        "config_alterada",
        f"{E.shield} Chat travado",
        CORES["aviso"],
        [("Chat", canal.mention), ("Por", f"{autor} ({autor.id})")],
    )
    return f"{E.correct} Chat travado. Para liberar, o dono usa /unlock."


async def destravar(canal, guild, autor) -> str:
    """/unlock (só dono): devolve o chat exatamente como estava antes do /lock."""
    from ...data import chats

    if isinstance(canal, discord.Thread):
        if not canal.locked:
            return f"{E.information} Esta thread não está travada."
        try:
            await canal.edit(locked=False)
        except discord.HTTPException as exc:
            return f"{E.wrong} Não consegui destravar: {exc}"
        chats.apagar(canal.id)
    elif guild is not None and isinstance(canal, discord.TextChannel):
        guardado = chats.pegar(canal.id)
        if guardado is None:
            return f"{E.information} Este chat não foi travado pelo /lock."
        padrao = guild.default_role
        if guardado["tinha_regra"]:
            regra = discord.PermissionOverwrite.from_pair(
                discord.Permissions(guardado["permite"]),
                discord.Permissions(guardado["nega"]),
            )
        else:
            regra = None
        try:
            await canal.set_permissions(
                padrao, overwrite=regra, reason="Chat destravado"
            )
        except discord.HTTPException as exc:
            return f"{E.wrong} Não consegui destravar: {exc}"
        chats.apagar(canal.id)
    else:
        return f"{E.wrong} Só dá para destravar canais de texto e threads."
    await notify.registrar(
        "config_alterada",
        f"{E.shield} Chat destravado",
        CORES["info"],
        [("Chat", canal.mention), ("Por", f"{autor} ({autor.id})")],
    )
    return f"{E.correct} Chat destravado."
