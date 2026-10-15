"""Pagamentos PIX: checkout do carrinho, acompanhamento até o desfecho e cancelamento."""

import asyncio
import base64
import io
import logging
import time
import math
from ..database.connection import connection
from . import locks

import discord

from ..config.settings import CORES
from ..ui import components
from ..data import coupons, orders, store
from ..services import rest
from . import carts, context, delivery, mercadopago, notify
from ..data import stock
from .. import catalogo
from ..ui.emojis import E

log = logging.getLogger("payments")

_polling: set[str] = set()
_tasks: set[asyncio.Task] = set()


def read_lote(message: discord.Message | None) -> int:
    """Lê a quantidade do botão 'Lote (Nx)' do carrinho, em qualquer nível de aninhamento."""

    def walk(node) -> int | None:
        label = getattr(node, "label", None)
        if (
            isinstance(label, str)
            and label.startswith("Lote (")
            and label.endswith("x)")
        ):
            value = label[6:-2]
            return int(value) if value.isascii() and value.isdigit() else 1
        for child in list(getattr(node, "children", None) or []) + list(
            getattr(node, "components", None) or []
        ):
            found = walk(child)
            if found is not None:
                return found
        return None

    if message is None:
        return 1
    for root in list(message.components or []):
        found = walk(root)
        if found is not None:
            return found
    return 1


def _checar_estoque(
    produto_key: str, quantidade: int, user_id: int | None = None
) -> None:
    produto = catalogo.produto(produto_key)
    if produto is None or produto.modelo.entrega == "manual":
        return
    disponivel = (
        stock.count_for_user(produto_key, user_id)
        if user_id is not None
        else stock.count_global(produto_key)
    )
    if disponivel > 0 and quantidade > disponivel:
        raise mercadopago.PixError(f"estoque insuficiente (disponível: {disponivel})")


def _intervalo_poll(decorrido: float) -> float:
    if decorrido < 180:
        return 5.0
    if decorrido < 600:
        return 10.0
    if decorrido < 1800:
        return 20.0
    return 60.0


async def _create_payment(pedido):
    payment = await mercadopago.create_pix(
        float(pedido["valor"]),
        pedido["checkout_descricao"]
        or f"Compra: {store.produto_nome(pedido['produto_key'])}",
        external_reference=f"ghost:{pedido['id']}",
        idempotency_key=pedido["checkout_key"],
        payer_email=pedido["checkout_email"],
    )
    orders.attach_payment(pedido["id"], payment["id"])
    return payment


async def checkout(interaction: discord.Interaction, produto_key: str) -> None:
    user, thread = interaction.user, interaction.channel
    async with locks.resource(f"cart:{thread.id}"):
        with connection() as conn:
            cart = conn.execute(
                "SELECT * FROM threads_ativas WHERE thread_id=?", (str(thread.id),)
            ).fetchone()
            approved = conn.execute(
                "SELECT 1 FROM pedidos WHERE thread_id=? AND status='approved'",
                (str(thread.id),),
            ).fetchone()
        if (
            cart is None
            or cart["tipo"] != "compra"
            or cart["produto_key"] != produto_key
            or cart["user_id"] != str(user.id)
            or approved
        ):
            raise mercadopago.PixError("esse carrinho não está disponível para você")
        if store.is_banned(user.id):
            raise mercadopago.PixError("você está impedido de realizar compras")
        produto = catalogo.produto(produto_key)
        if produto is None or not produto.ativo:
            raise mercadopago.PixError("produto indisponível")
        pending = orders.pending_in_thread(thread.id)
        if pending:
            pedido = orders.get(pending[0]["id"])
            if pedido["payment_id"] and pedido["pix_publicado"]:
                raise mercadopago.PixError("já existe um PIX pendente neste carrinho")
        else:
            quantidade = int(cart["quantidade"])
            if not 1 <= quantidade <= 999:
                raise mercadopago.PixError("quantidade inválida")
            _checar_estoque(produto_key, quantidade, user.id)
            unit = store.preco(produto_key)
            valor = round(unit * quantidade, 2)
            cupom = carts.get_cupom(thread.id)
            atual = coupons.get(cupom["codigo"]) if cupom else None
            if cupom:
                erro = (
                    coupons.avaliar(
                        atual, user.id, unit, [r.id for r in getattr(user, "roles", [])]
                    )
                    if atual
                    else "Cupom removido."
                )
                if erro:
                    carts.clear_cupom(thread.id)
                    raise mercadopago.PixError(erro)
                valor = round(valor - coupons.desconto(atual, valor), 2)
            if not math.isfinite(valor) or valor <= 0:
                raise mercadopago.PixError("o total precisa ser pelo menos R$ 0,01")
            # Nenhum await entre validação e reserva: limite de cupom não pode ser ultrapassado por outro checkout.
            with connection():
                pedido_id = orders.create_pending(
                    None, produto_key, valor, quantidade, user.id, thread.id
                )
                if atual:
                    coupons.register_use(atual["id"], user.id, pedido_id)
            pedido = orders.get(pedido_id)
        pedido_id, quantidade, valor = (
            pedido["id"],
            int(pedido["quantidade"]),
            float(pedido["valor"]),
        )
        try:
            payment = (
                await mercadopago.get_payment(pedido["payment_id"])
                if pedido["payment_id"]
                else await _create_payment(pedido)
            )
        except mercadopago.PixError as exc:
            if (
                exc.status is not None
                and 400 <= exc.status < 500
                and exc.status not in (408, 409, 429)
            ):
                orders.set_status(pedido_id, "rejected")
                coupons.refund_for_pedido(pedido_id)
            # Timeout/5xx é resultado incerto: o próximo clique reutiliza a chave persistida.
            raise
        payment_id = str(payment["id"])
        start_polling(
            thread, pedido_id, payment_id, user
        )  # roda mesmo se a mensagem do PIX falhar
        carts.clear_cupom(thread.id)
        await notify.registrar(
            "pix_gerado",
            f"{E.pix} PIX gerado",
            CORES["aviso"],
            [
                ("Cliente", f"{user} ({user.id})"),
                ("Pedido", f"#{pedido_id}"),
                ("Quantidade", quantidade),
                ("Valor", f"R$ {valor:.2f}"),
            ],
        )
        tx = (payment.get("point_of_interaction") or {}).get("transaction_data") or {}
        texto = f"## {E.pix} Pagamento PIX\n\nCódigo copia e cola:\n`{tx.get('qr_code', 'N/A')}`\n\nAguardando confirmação..."
        view = components.pix_view(payment_id)
        encoded = tx.get("qr_code_base64")
        if encoded and len(encoded) <= 2_000_000:
            try:
                data = base64.b64decode(encoded, validate=True)
            except ValueError:
                data = None
            if data:
                await interaction.followup.send(
                    content=texto,
                    file=discord.File(io.BytesIO(data), "pix_qr.png"),
                    view=view,
                )
            else:
                await interaction.followup.send(content=texto, view=view)
        else:
            await interaction.followup.send(content=texto, view=view)
        with connection() as conn:
            conn.execute("UPDATE pedidos SET pix_publicado=1 WHERE id=?", (pedido_id,))
        await rest.edit_message(
            thread.id,
            interaction.message.id,
            {
                "flags": 1 << 15,
                "components": [
                    {"type": 17, "components": _cart_after_checkout(produto_key)}
                ],
            },
        )


def _cart_after_checkout(produto_key: str) -> list[dict]:
    inner: list[dict] = [
        {"type": 10, "content": f"## {store.produto_nome(produto_key)}"},
        {"type": 14, "divider": True, "spacing": 1},
    ]
    img = carts.image_for(produto_key)
    if img:
        inner += [
            {"type": 12, "items": [{"media": {"url": img}}]},
            {"type": 14, "divider": True, "spacing": 1},
        ]
    return inner


def start_polling(
    thread: discord.Thread, pedido_id: int, payment_id: str, user: discord.abc.User
) -> None:
    if payment_id in _polling:
        return
    _polling.add(payment_id)
    task = asyncio.create_task(_poll(thread, pedido_id, payment_id, user))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def _poll(thread, pedido_id, payment_id, user) -> None:
    try:
        inicio = time.monotonic()
        # Sem prazo local: o pedido pendente só é encerrado por um admin ou por um estado final do Mercado Pago.
        while True:
            pedido = orders.get(pedido_id)
            if pedido is None or pedido["status"] != "pending":
                return
            await asyncio.sleep(_intervalo_poll(time.monotonic() - inicio))
            try:
                payment = await mercadopago.get_payment(payment_id)
                estado = mercadopago.normalize_status(payment)
                if estado == "approved" and (
                    str(payment.get("id")) != str(payment_id)
                    or abs(
                        float(payment.get("transaction_amount", -1))
                        - float(pedido["valor"])
                    )
                    > 0.001
                    or payment.get("currency_id") != "BRL"
                ):
                    log.error(
                        "pagamento %s não corresponde ao pedido %s",
                        payment_id,
                        pedido_id,
                    )
                    return
            except mercadopago.PixError as exc:
                if exc.status == 404:
                    await _finish_failed(
                        thread,
                        pedido_id,
                        "expired",
                        f"{E.clock} O PIX expirou (pagamento não encontrado).",
                    )
                    return
                log.warning("poll %s: %s", payment_id, exc)
                continue
            if estado == "approved":
                await _approve(thread, pedido_id, user)
                return
            if estado in ("cancelled", "rejected", "refunded", "expired"):
                await _finish_failed(thread, pedido_id, estado, _mensagem_falha(estado))
                return
    except Exception:
        log.exception("erro no acompanhamento do pagamento %s", payment_id)
    finally:
        _polling.discard(payment_id)


def _mensagem_falha(estado: str) -> str:
    return {"expired": f"{E.clock} O PIX expirou (status_detail=expired)."}.get(
        estado, f"{E.wrong} Pagamento {estado}."
    )


async def _approve(
    thread: discord.Thread, pedido_id: int, user: discord.abc.User, aprovado_por=None
) -> None:
    pedido = orders.get(pedido_id)
    if pedido is None or pedido["status"] != "pending":
        return
    if not orders.set_status(pedido_id, "approved"):
        return
    campos = [
        ("Cliente", f"{user} ({user.id})"),
        ("Pedido", f"#{pedido_id}"),
        ("Thread", thread.name),
    ]
    if aprovado_por is not None:
        campos.append(("Aprovado por", f"{aprovado_por} ({aprovado_por.id})"))
    await notify.registrar(
        "pagamento_aprovado",
        f"{E.correct} Pagamento aprovado",
        CORES["sucesso"],
        campos,
    )
    await delivery.deliver_approved(thread, orders.get(pedido_id), user)
    if thread.guild is not None:
        from ..bot.secoes import (
            cargos,
        )  # import tardio: evita ciclo entre services e bot

        await cargos.sincronizar(thread.guild, user.id)


async def aprovar_manual(
    thread: discord.Thread, pedido_id: int, admin: discord.abc.User
) -> str | None:
    """Aprova um pedido pendente como se o PIX tivesse sido pago. Devolve o motivo se não for possível."""
    pedido = orders.get(pedido_id)
    if pedido is None or pedido["status"] != "pending":
        return "Esse pedido não está mais pendente."
    if pedido["payment_id"] and not await mercadopago.cancel_payment(
        pedido["payment_id"]
    ):
        return "Não consegui cancelar o PIX no Mercado Pago, então o pedido não foi aprovado (evita cobrança dupla)."
    user = await context.user(int(pedido["discord_user_id"]))
    with connection() as conn:
        conn.execute(
            "UPDATE pedidos SET payment_id=? WHERE id=? AND status='pending'",
            (f"manual:{admin.id}:{pedido_id}", pedido_id),
        )
    await _approve(thread, pedido_id, user, aprovado_por=admin)
    return None


async def _finish_failed(thread, pedido_id: int, estado: str, mensagem: str) -> None:
    pedido = orders.get(pedido_id)
    if pedido is None or pedido["status"] != "pending":
        return
    with connection():
        if not orders.set_status(pedido_id, estado):
            return
        coupons.refund_for_pedido(pedido_id)
    await notify.registrar(
        "pix_encerrado",
        f"{E.wrong} PIX encerrado sem pagamento",
        CORES["erro"],
        [("Pedido", f"#{pedido_id}"), ("Motivo", estado), ("Thread", thread.name)],
    )
    carts.clear_cupom(thread.id)
    try:
        await thread.send(mensagem)
    except discord.HTTPException:
        pass


async def cancel_by_user(interaction: discord.Interaction, payment_id: str) -> None:
    pedido = orders.get_by_payment(payment_id)
    if (
        pedido is None
        or pedido["status"] != "pending"
        or pedido["discord_user_id"] != str(interaction.user.id)
        or pedido["thread_id"] != str(interaction.channel_id)
    ):
        await interaction.followup.send(
            f"{E.alert} Esse PIX não pode mais ser cancelado.", ephemeral=True
        )
        return
    if not await mercadopago.cancel_payment(payment_id):
        await interaction.followup.send(
            f"{E.wrong} Não consegui cancelar o PIX agora. Tente de novo.",
            ephemeral=True,
        )
        return
    with connection():
        if not orders.set_status(pedido["id"], "cancelled"):
            return
        coupons.refund_for_pedido(pedido["id"])
    await notify.registrar(
        "pix_cancelado",
        f"{E.wrong} PIX cancelado pelo cliente",
        CORES["aviso"],
        [
            ("Pedido", f"#{pedido['id']}"),
            ("Usuário", f"{interaction.user} ({interaction.user.id})"),
        ],
    )
    if pedido["thread_id"]:
        carts.clear_cupom(pedido["thread_id"])
        store.forget_thread(pedido["thread_id"])
    try:
        await interaction.channel.delete()
    except discord.HTTPException:
        pass


async def copy_code(interaction: discord.Interaction, payment_id: str) -> None:
    pedido = orders.get_by_payment(payment_id)
    if (
        pedido is None
        or pedido["discord_user_id"] != str(interaction.user.id)
        or pedido["thread_id"] != str(interaction.channel_id)
    ):
        await interaction.response.send_message(
            "Sem permissão para esse PIX.", ephemeral=True
        )
        return
    await interaction.response.defer(ephemeral=True)
    payment = await mercadopago.get_payment(payment_id)
    tx = (payment.get("point_of_interaction") or {}).get("transaction_data") or {}
    await interaction.followup.send(
        tx.get("qr_code") or "Código indisponível.", ephemeral=True
    )


def resume() -> None:
    with connection() as conn:
        rows = conn.execute(
            "SELECT * FROM pedidos WHERE thread_id IS NOT NULL AND (status='pending' OR (status='approved' AND entrega_status IN ('pending','processing')))"
        ).fetchall()
    for row in rows:
        task = asyncio.create_task(_resume_one(row))
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)


async def _resume_one(row) -> None:
    try:
        thread = await context.channel(int(row["thread_id"]))
        user = await context.user(int(row["discord_user_id"]))
    except discord.HTTPException:
        log.warning("pedido %s não retomado: thread ou usuário indisponível", row["id"])
        return
    if row["status"] == "approved":
        await delivery.deliver_approved(thread, row, user)
    else:
        async with locks.resource(f"cart:{thread.id}"):
            current = orders.get(row["id"])
            if current is None or current["status"] != "pending":
                return
            payment_id = current["payment_id"]
            if not payment_id and current["checkout_key"]:
                try:
                    payment_id = str((await _create_payment(current))["id"])
                except mercadopago.PixError:
                    log.exception(
                        "PIX %s à reprendre; aucun nouveau paiement créé", row["id"]
                    )
                    return
            if payment_id:
                start_polling(thread, row["id"], payment_id, user)


async def close():
    for task in tuple(_tasks):
        task.cancel()
    await asyncio.gather(*_tasks, return_exceptions=True)
    _tasks.clear()
