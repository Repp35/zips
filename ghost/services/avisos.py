"""Avisos de estoque: "me avise quando chegar", reposição, novos produtos e fechamento de pedidos de restock."""

import logging

import discord

from ..config.settings import CORES
from .. import catalogo
from ..ui import layout
from ..config import settings
from ..data import stock, store
from ..catalogo import render
from . import context, rest
from ..ui.emojis import E

log = logging.getLogger("avisos")


def inscrever(user_id: int, produto_key: str) -> bool:
    return store.inscrever_aviso(user_id, produto_key)


def pedido_restock_limit_reached() -> bool:
    return store.restock_pending_last_day() >= settings.RESTOCK_PEDIDOS_DIA


async def estoque_reposto(produto_key: str, adicionados: int) -> None:
    """Produto saiu de zero estoque: fecha pedidos de restock, avisa o canal e os inscritos."""
    nome = store.produto_nome(produto_key)
    total = stock.count_global(produto_key)
    for thread_id in store.conclude_restock(produto_key):
        try:
            await (await context.channel(int(thread_id))).delete()
        except discord.HTTPException:
            pass

    canal = store.channel_for("AVISO_RESTOCK")
    if canal:
        await _postar(
            canal,
            _cartao(
                f"{E.bell} Voltou ao estoque",
                f"**{nome}** foi reabastecido.",
                [("Adicionados", f"{adicionados}x"), ("Estoque total", f"{total}x")],
            ),
        )

    enviados = 0
    for uid in store.avisos_do_produto(produto_key):
        before = enviados
        try:
            usuario = await context.user(uid)
            await rest.send_dm(
                usuario,
                _cartao(
                    f"{E.bell} Voltou ao estoque",
                    f"**{nome}** voltou ao estoque!",
                    [
                        ("Adicionados", f"{adicionados}x"),
                        ("Estoque total", f"{total}x"),
                    ],
                ),
            )
            enviados += 1
        except (rest.DiscordHTTPError, discord.HTTPException) as exc:
            if getattr(exc, "status", None) == 403 and canal:
                if await _postar(
                    canal, {"content": f"<@{uid}> — **{nome}** voltou ao estoque!"}
                ):
                    enviados += 1
            else:
                log.warning("falha ao avisar %s sobre %s: %s", uid, produto_key, exc)
        if enviados > before:
            store.remover_aviso(uid, produto_key)
    log.info("reposição de %s: %d avisos enviados", produto_key, enviados)


async def anunciar_novidades() -> None:
    """Anuncia no canal de avisos os produtos do catálogo que ainda não foram anunciados.

    Na primeira execução (banco sem nenhum anúncio) o catálogo atual é registrado sem anunciar,
    para não postar todos os produtos de uma vez.
    """
    anunciados = store.produtos_anunciados()
    ativos = catalogo.produtos()
    if not anunciados:
        for produto in ativos:
            store.marcar_anunciado(produto.id)
        log.info("catálogo inicial registrado: %d produtos", len(ativos))
        return

    canal = store.channel_for("AVISO_RESTOCK")
    if not canal:
        return
    for produto in ativos:
        if not produto.anunciar or produto.id in anunciados:
            continue
        cartao = _cartao(
            f"{E.cardbox} Novo produto",
            f"**{produto.nome}**\n{render.brl(store.preco(produto.id))}",
            [],
            imagem=produto.imagem,
        )
        if await _postar(canal, cartao):
            store.marcar_anunciado(produto.id)
            log.info("novo produto anunciado: %s", produto.id)


def _cartao(
    titulo: str,
    descricao: str,
    campos: list[tuple[str, str]],
    imagem: str | None = None,
) -> dict:
    blocos = [layout.titulo(titulo), layout.texto(descricao)]
    if campos:
        blocos.append(layout.texto(layout.pares(campos)))
    if imagem:
        blocos.insert(0, layout.imagem(imagem))
    blocos.append(layout.rodape(layout.carimbo()))
    return layout.mensagem(layout.cartao(*blocos, cor=CORES["info"]))


async def _postar(canal_id: int, payload: dict) -> bool:
    try:
        await rest.send_message(canal_id, payload)
        return True
    except RuntimeError as exc:
        log.warning("falha ao postar no canal %s: %s", canal_id, exc)
        return False
