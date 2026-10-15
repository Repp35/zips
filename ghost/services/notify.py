"""Logs de auditoria, anúncios de venda, equipe em threads e cargos de cliente."""

import asyncio
import logging

import discord

from ..config.settings import CORES
from ..core import diario, eventos
from ..ui import layout
from ..config import settings
from ..catalogo import render
from ..data import orders, store
from . import papeis
from ..ui.emojis import E
from . import rest

log = logging.getLogger("notify")


async def send_log(payload: dict) -> None:
    from . import incidente

    if incidente.ativo():
        return  # registrar() já gravou o evento no diário persistente
    if store.ajuste("logs_ativo") == "0":
        return
    channel_id = int(store.ajuste("logs_channel_id") or settings.LOGS_CHANNEL_ID or 0)
    if not channel_id:
        return
    try:
        await rest.send_message(channel_id, payload)
    except RuntimeError as exc:
        log.error("falha ao enviar log: %s", exc)


async def ephemera(
    interaction: discord.Interaction,
    content: str | None = None,
    *,
    view=None,
    segundos: int = settings.EFEMERA_S,
) -> None:
    """Mensagem só para quem clicou que some sozinha. O Discord só permite apagar até 15 min após a interação."""
    kwargs = {"ephemeral": True}
    if content is not None:
        kwargs["content"] = content
    if view is not None:
        kwargs["view"] = view
    if interaction.response.is_done():
        msg = await interaction.followup.send(wait=True, **kwargs)
        apagar = lambda: interaction.followup.delete_message(msg.id)
    else:
        await interaction.response.send_message(**kwargs)
        apagar = interaction.delete_original_response
    asyncio.create_task(_apagar_depois(apagar, segundos))


async def _apagar_depois(apagar, segundos: int) -> None:
    await asyncio.sleep(segundos)
    try:
        await apagar()
    except discord.HTTPException:
        pass


async def registrar(evento: str, titulo: str, cor: int, campos) -> None:
    """Registra um evento no canal de logs, se a equipe o tiver deixado ligado. Nunca derruba o fluxo principal."""
    try:
        diario.evento(evento, titulo, campos)
        if not eventos.ativo(evento):
            return
        await send_log(layout.log(titulo, cor, [(n, str(v)) for n, v in campos]))
    except Exception:
        log.exception("falha ao registrar log: %s", titulo)


async def send_sale(user: discord.abc.User, produto_key: str, valor: float) -> None:
    if store.is_admin(user.id) or store.is_staff(user.id):
        return
    log.info("venda: %s (%s) %s R$ %.2f", user.name, user.id, produto_key, valor)
    if not settings.SALES_CHANNEL_ID:
        return
    payload = layout.mensagem(
        layout.cartao(
            layout.titulo(f"{E.basket} Venda realizada"),
            layout.texto(f"**{user.name}** recebeu o pedido."),
            layout.separador(),
            layout.texto(
                layout.pares(
                    [
                        ("Produto", store.produto_nome(produto_key)),
                        ("Valor", render.brl(valor)),
                    ]
                )
            ),
            cor=CORES["sucesso"],
        )
    )
    try:
        await rest.send_message(settings.SALES_CHANNEL_ID, payload)
    except RuntimeError as exc:
        log.error("falha ao anunciar venda: %s", exc)


async def add_team_to_thread(
    thread: discord.Thread, excluir_id: int | None = None
) -> None:
    async def um(uid: int) -> None:
        try:
            await thread.add_user(discord.Object(id=uid))
        except discord.HTTPException:
            pass

    await asyncio.gather(*(um(u) for u in store.team_ids() if u != excluir_id))


def nivel_atual(total: float, niveis: list[dict]) -> dict | None:
    """Maior nível cujo valor mínimo o cliente já alcançou."""
    alcancados = [n for n in niveis if total >= float(n["valor"])]
    return max(alcancados, key=lambda n: float(n["valor"])) if alcancados else None


async def sync_client_roles(user_id: int, guild: discord.Guild) -> None:
    """Dá o cargo do nível alcançado e tira os cargos de níveis anteriores (o nível 0 fica sempre)."""
    try:
        member = guild.get_member(user_id) or await guild.fetch_member(user_id)
    except discord.HTTPException:
        return
    niveis = store.niveis_cargo()
    atual = nivel_atual(orders.approved_total(user_id), niveis)
    if atual is None:
        return
    tem = {r.id for r in member.roles}
    base = {papeis.id_do_nivel(n) for n in niveis if float(n["valor"]) == 0}
    desejados = (base | {papeis.id_do_nivel(atual)}) - {None}
    antigos = {
        papeis.id_do_nivel(n)
        for n in niveis
        if float(n["valor"]) > 0 and papeis.id_do_nivel(n) != papeis.id_do_nivel(atual)
    } - {None}
    for cid in antigos & tem:
        role = guild.get_role(cid)
        if role is not None:
            try:
                await member.remove_roles(role)
            except discord.HTTPException as exc:
                log.warning("falha ao remover cargo %s: %s", cid, exc)
    for cid in desejados - tem:
        role = guild.get_role(cid)
        if role is not None:
            try:
                await member.add_roles(role)
            except discord.HTTPException as exc:
                log.warning("falha ao adicionar cargo %s: %s", cid, exc)
