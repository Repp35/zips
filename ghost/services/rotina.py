"""Ações do dia a dia da equipe: entrega manual de itens de estoque.

Regra: um item só sai do estoque definitivamente depois que a DM foi entregue. Se a DM falhar,
os itens ainda não enviados voltam ao estoque. Assim o cliente nunca perde produto.
"""

import logging
from typing import Awaitable, Callable

from .. import catalogo
from ..data import orders, stock
from . import delivery, notify, shelves
from ..database.connection import connection

log = logging.getLogger("rotina")


async def entregar_itens(
    produto_key: str,
    user,
    quantidade: int,
    valor_unitario: float,
    guild,
    admin_id: int,
    enviar: Callable[..., Awaitable[bool]] | None = None,
) -> tuple[int, str | None]:
    """Reserva, envia e confirma os itens. Devolve (entregues, motivo_da_falha ou None)."""
    enviar = enviar or delivery.enviar_dm
    reservados: list[dict] = []
    for _ in range(quantidade):
        item = stock.reserve(produto_key, user.id)
        if item is None:
            break
        reservados.append(item)
    if len(reservados) < quantidade:
        for item in reservados:
            stock.release(item)
        return 0, f"estoque insuficiente (disponível: {len(reservados)})"

    produto = catalogo.produto(produto_key)
    nome = produto.nome if produto else produto_key
    entregues = 0
    for i, item in enumerate(reservados):
        payload = delivery.build_entrega(nome, valor_unitario, item, guild)
        try:
            enviado = await enviar(user, payload)
        except BaseException:
            # Este item pode ter sido recebido: mantém a reserva para conferência.
            # Reserva manual não entra na liberação automática enquanto for incerta.
            with connection() as conn:
                table = "estoque_links" if item["kind"] == "link" else "estoque"
                conn.execute(
                    f"UPDATE {table} SET reservado_em=NULL WHERE id=?", (item["id"],)
                )
            for remaining in reservados[i + 1 :]:
                stock.release(remaining)
            if entregues:
                orders.registrar_entrega_manual(
                    user.id,
                    produto_key,
                    valor_unitario * entregues,
                    entregues,
                    admin_id,
                )
            raise
        if not enviado:
            break
        stock.confirm(item, user.id, produto_key)
        entregues += 1
    for item in reservados[entregues:]:
        stock.release(item)

    if entregues:
        orders.registrar_entrega_manual(
            user.id, produto_key, valor_unitario * entregues, entregues, admin_id
        )
        await shelves.refresh_for_product(produto_key)
        if guild is not None:
            await notify.sync_client_roles(user.id, guild)
    if entregues < quantidade:
        log.info(
            "entrega manual parcial de %s para %s: %d de %d",
            produto_key,
            user.id,
            entregues,
            quantidade,
        )
        return entregues, "a DM do cliente está fechada"
    return entregues, None
