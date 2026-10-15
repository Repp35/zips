"""Entrega após pagamento aprovado: reserva do estoque, envio por DM ou thread, e confirmação."""

from ..database.connection import connection

import logging

import discord

from ..config.settings import CORES
from .. import catalogo
from ..config import settings
from ..ui import components
from ..catalogo import render
from ..data import orders, stock, store
from . import context, estoque_dm, notify, rest, scheduler, shelves
from ..ui.emojis import E

log = logging.getLogger("delivery")


def build_entrega(
    produto_nome: str,
    valor: float,
    item: dict | None,
    guild: discord.Guild | None,
    topo: str | None = None,
    acoes: list[dict] | None = None,
) -> dict:
    """Mensagem de entrega (DM ou thread). Com item, monta a mensagem com os dados do item (estoque_dm); sem item,
    é o aviso de entrega manual."""
    nome_loja = guild.name if guild else settings.NOME_LOJA_PADRAO
    payload = estoque_dm.renderizar(
        item, produto_nome, valor, nome_loja, CORES["entrega"]
    )
    if guild and guild.icon and payload["embeds"][0].get("footer") is not None:
        payload["embeds"][0]["footer"]["icon_url"] = guild.icon.url
    if topo:
        payload["content"] = (
            f"{payload['content']}\n{topo}" if payload.get("content") else topo
        )
    if acoes:
        payload["components"] = [{"type": 1, "components": acoes}]
    return payload


async def _send_dm(user: discord.abc.User, payload: dict) -> bool:
    try:
        await rest.send_dm(user, payload)
        return True
    except (RuntimeError, discord.HTTPException) as exc:
        if (
            isinstance(exc, rest.DiscordTransportError)
            or getattr(exc, "status", 0) >= 500
        ):
            raise
        log.info("DM falhou para %s: %s", user.id, exc)
        return False


async def enviar_dm(user: discord.abc.User, payload: dict) -> bool:
    """Envia uma mensagem de entrega na DM do cliente. Devolve False se a DM estiver fechada."""
    return await _send_dm(user, payload)


async def send_item(
    user: discord.abc.User,
    produto_key: str,
    item: dict,
    valor: float,
    thread_fallback: discord.Thread | None,
) -> str:
    guild = context.bot().get_guild(settings.GUILD_ID) if settings.GUILD_ID else None
    nome = store.produto_nome(produto_key)
    if await _send_dm(user, build_entrega(nome, valor, item, guild)):
        return "dm"
    if thread_fallback:
        topo = (
            f"{user.mention} {E.alert} Não conseguimos enviar na sua DM (ela está fechada), "
            "por isso o produto está aqui na thread.\n"
            "Guarde-o com segurança. **Esta thread não fecha sozinha**: quando tiver recebido tudo, "
            "clique em **Concluído** para encerrá-la."
        )
        botao = render.botao(
            "Concluído", f"entrega_concluida_{thread_fallback.id}", 3, "@correct"
        )
        try:
            await rest.send_message(
                thread_fallback.id,
                build_entrega(nome, valor, item, guild, topo=topo, acoes=[botao]),
            )
            return "thread"
        except RuntimeError as exc:
            if (
                isinstance(exc, rest.DiscordTransportError)
                or getattr(exc, "status", 0) >= 500
            ):
                raise
    return "failed"


async def deliver_approved(
    thread: discord.Thread, pedido, user: discord.abc.User
) -> None:
    produto_key, quantidade, pedido_id = (
        pedido["produto_key"],
        int(pedido["quantidade"]),
        pedido["id"],
    )
    valor = float(pedido["valor"])
    orders.release_unstarted(pedido_id)
    orders.set_delivery(pedido_id, "processing")
    if orders.uncertain_delivery(pedido_id):
        await _deliver_manual(thread, pedido_id, produto_key)
        return
    quantidade -= orders.delivered_count(pedido_id)
    if quantidade <= 0:
        orders.set_delivery(pedido_id, "completed")
        return
    unit = round(valor / int(pedido["quantidade"]), 2)
    produto = catalogo.produto(produto_key)
    if produto is None or produto.modelo.entrega == "manual":
        await _deliver_manual(thread, pedido_id, produto_key)
        return

    reservados = []
    for _ in range(quantidade):
        with connection():
            item = stock.reserve(produto_key, user.id)
            if item is not None:
                orders.record_delivery(pedido_id, item, "reserved")
        if item is None:
            break
        reservados.append(item)
    if not reservados:
        await _deliver_manual(thread, pedido_id, produto_key)
        return

    resultados = []
    for item in reservados:
        orders.record_delivery(pedido_id, item, "sending")
        try:
            resultado = await send_item(user, produto_key, item, unit, thread)
        except BaseException:
            # Envio pode ter chegado ao Discord: não devolva este item ao estoque.
            orders.set_delivery(pedido_id, "manual")
            store.mark_manual(thread.id)
            await notify.registrar(
                "entrega_falha",
                "Entrega com resultado incerto; confira antes de reenviar",
                CORES["aviso"],
                [
                    ("Pedido", f"#{pedido_id}"),
                    ("Item", item["id"]),
                    ("Tipo", item["kind"]),
                ],
            )
            for outro in reservados[reservados.index(item) + 1 :]:
                stock.release(outro)
                orders.record_delivery(pedido_id, outro, "failed")
            raise
        if resultado == "failed":
            stock.release(item)
            orders.record_delivery(pedido_id, item, "failed")
            log.warning(
                "entrega falhou, reserva liberada: item=%s produto=%s",
                item["id"],
                produto_key,
            )
        else:
            with connection():
                stock.confirm(item, user.id, produto_key)
                orders.mark_delivered_stock(pedido_id, item["id"])
                orders.record_delivery(pedido_id, item, "sent")
        resultados.append(resultado)

    if all(r == "failed" for r in resultados):
        await _deliver_manual(thread, pedido_id, produto_key)
        await thread.send(
            f"{E.alert} Pagamento aprovado, mas houve um erro na entrega. "
            "Nenhuma conta ficou presa — abra um ticket de suporte para receber seu produto."
        )
        await notify.registrar(
            "entrega_falha",
            f"{E.alert} Entrega automática falhou totalmente",
            CORES["aviso"],
            [
                ("Thread", thread.name),
                ("User", f"{user} ({user.id})"),
                ("Produto", store.produto_nome(produto_key)),
                ("Pedido", f"#{pedido_id}"),
            ],
        )
        return

    falta = quantidade - sum(r != "failed" for r in resultados)
    if falta > 0:
        await _entrega_parcial(thread, pedido_id, produto_key, falta)
        await _refresh_shelf_after_sale(produto_key)
        return

    orders.set_delivery(pedido_id, "completed")
    if all(r == "dm" for r in resultados):
        await thread.send(
            f"{E.correct} Pagamento aprovado! Seu produto foi enviado na sua DM. "
            "Vou fechar essa thread em alguns segundos. "
            "Caso não localize, abra um ticket no suporte."
        )
        scheduler.schedule_close(
            thread.id, settings.ENTREGA_DM_FECHA_S, "entrega automática (DM ok)"
        )
    else:
        await thread.send(
            f"{E.correct} Pagamento aprovado! Como não conseguimos enviar na sua DM, "
            "o produto foi postado aqui na thread. **Esta thread não fecha sozinha**: "
            "clique em **Concluído** quando tiver recebido."
        )

    modo = "DM" if all(r == "dm" for r in resultados) else "thread"
    await notify.registrar(
        "entrega_automatica",
        f"{E.correct} Entrega automática",
        CORES["sucesso"],
        [
            ("Cliente", f"{user} ({user.id})"),
            ("Pedido", f"#{pedido_id}"),
            ("Produto", store.produto_nome(produto_key)),
            ("Quantidade", quantidade),
            ("Valor", f"R$ {valor:.2f}"),
            ("Enviado por", modo),
        ],
    )
    if orders.claim_sale(pedido_id):
        await notify.send_sale(user, produto_key, valor)
    await _refresh_shelf_after_sale(produto_key)
    if thread.guild:
        await notify.sync_client_roles(user.id, thread.guild)


async def _deliver_manual(
    thread: discord.Thread, pedido_id: int, produto_key: str
) -> None:
    orders.set_delivery(pedido_id, "manual")
    store.mark_manual(thread.id)
    await notify.registrar(
        "entrega_manual_pendente",
        f"{E.alert} Entrega manual pendente",
        CORES["aviso"],
        [
            ("Pedido", f"#{pedido_id}"),
            ("Produto", store.produto_nome(produto_key)),
            ("Thread", thread.name),
        ],
    )
    await thread.send(
        f"{E.correct} Pagamento aprovado!\n"
        f"{E.cardbox} Esse produto é de entrega **manual** (estoque esgotado) — "
        "um membro da equipe vai te chamar por aqui.\n\n"
        "_Quando o cliente receber o produto, clique em **Concluído** para registrar a venda e fechar a thread. "
        "**Fechar e Salvar** só encerra a thread sem registrar venda._",
        view=components.manual_delivery_view(pedido_id),
    )


async def _entrega_parcial(
    thread: discord.Thread, pedido_id: int, produto_key: str, falta: int
) -> None:
    """Estoque acabou no meio da compra: entrega o que foi reservado e marca o restante como manual."""
    orders.set_delivery(pedido_id, "manual")
    store.mark_manual(thread.id)
    await thread.send(
        f"{E.alert} Pagamento aprovado, mas o estoque acabou durante a compra: "
        f"**{falta}** unidade(s) ficará(ão) para entrega **manual**. "
        "Um membro da equipe vai te chamar por aqui para concluir.\n\n"
        "_Quando o cliente receber o restante, clique em **Concluído** para registrar a venda._",
        view=components.manual_delivery_view(pedido_id),
    )
    await notify.registrar(
        "entrega_parcial",
        f"{E.alert} Entrega Parcial",
        CORES["aviso"],
        [
            ("Thread", thread.name),
            ("Produto", store.produto_nome(produto_key)),
            ("Pendentes", str(falta)),
            ("Pedido", f"#{pedido_id}"),
        ],
    )


async def _refresh_shelf_after_sale(produto_key: str) -> None:
    await shelves.refresh_for_product(produto_key)
