"""Pedidos (compras via PIX) e uso de cupons."""

from ..database.connection import connection
import uuid
import math

# Status "refunded" é gravado na própria coluna pedidos.status (TEXT), sem migração de tabela.
STATUS_APROVADO = "approved"
STATUS_REEMBOLSADO = "refunded"


def create_pending(
    payment_id: str,
    produto_key: str,
    valor: float,
    quantidade: int,
    user_id: int,
    thread_id: int,
) -> int:
    if not 1 <= quantidade <= 999 or not math.isfinite(valor) or valor <= 0:
        raise ValueError("quantidade ou valor inválido")
    from ..config import settings
    from . import store

    with connection() as conn:
        cur = conn.execute(
            "INSERT INTO pedidos (discord_user_id, produto_key, valor, payment_id, status, thread_id, quantidade, checkout_key, entrega_status, checkout_descricao, checkout_email) "
            "VALUES (?,?,?,?,'pending',?,?,?,'pending',?,?)",
            (
                str(user_id),
                produto_key,
                valor,
                payment_id,
                str(thread_id),
                quantidade,
                uuid.uuid4().hex,
                f"Compra: {store.produto_nome(produto_key)}"[:200],
                settings.PAYER_EMAIL,
            ),
        )
        return cur.lastrowid


def get(pedido_id: int):
    with connection() as conn:
        return conn.execute("SELECT * FROM pedidos WHERE id=?", (pedido_id,)).fetchone()


def get_by_payment(payment_id: str):
    with connection() as conn:
        return conn.execute(
            "SELECT * FROM pedidos WHERE payment_id=?", (str(payment_id),)
        ).fetchone()


def pending_with_thread():
    with connection() as conn:
        return conn.execute(
            "SELECT * FROM pedidos WHERE status='pending' AND thread_id IS NOT NULL"
        ).fetchall()


def set_status(pedido_id: int, status: str, *, only_pending: bool = True) -> bool:
    sql = "UPDATE pedidos SET status=? WHERE id=?"
    if only_pending:
        sql += " AND status='pending'"
    with connection() as conn:
        return conn.execute(sql, (status, pedido_id)).rowcount == 1


def pending_in_thread(thread_id: str):
    with connection() as conn:
        return conn.execute(
            "SELECT id, payment_id FROM pedidos WHERE thread_id=? AND status='pending'",
            (str(thread_id),),
        ).fetchall()


def approved_total(user_id: int) -> float:
    with connection() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(valor), 0) AS total FROM pedidos WHERE discord_user_id=? AND status='approved'",
            (str(user_id),),
        ).fetchone()
    return float(row["total"])


def has_approved(user_id: int) -> bool:
    with connection() as conn:
        return bool(
            conn.execute(
                "SELECT 1 FROM pedidos WHERE discord_user_id=? AND status='approved' LIMIT 1",
                (str(user_id),),
            ).fetchone()
        )


def last_approved(user_id: int, produto_key: str):
    with connection() as conn:
        return conn.execute(
            "SELECT id, valor, criado_em FROM pedidos WHERE discord_user_id=? AND produto_key=? "
            "AND status='approved' ORDER BY criado_em DESC LIMIT 1",
            (str(user_id), produto_key),
        ).fetchone()


def recent_products(user_id: int, dias: int = 7) -> list[str]:
    with connection() as conn:
        rows = conn.execute(
            "SELECT DISTINCT produto_key FROM pedidos WHERE discord_user_id=? AND status='approved' "
            "AND criado_em > datetime('now', ?)",
            (str(user_id), f"-{dias} days"),
        ).fetchall()
    return [r["produto_key"] for r in rows]


def mark_delivered_stock(pedido_id: int, estoque_id: int) -> None:
    with connection() as conn:
        conn.execute(
            "UPDATE pedidos SET estoque_id=? WHERE id=?", (estoque_id, pedido_id)
        )


def registrar_entrega_manual(
    user_id: int, produto_key: str, valor: float, quantidade: int, admin_id: int
) -> int:
    """Entrega feita pela equipe (/entregar). Já nasce aprovada e sem thread, então não entra na fila de pagamentos."""
    with connection() as conn:
        cur = conn.execute(
            "INSERT INTO pedidos (discord_user_id, produto_key, valor, quantidade, payment_id, status) "
            "VALUES (?,?,?,?,?, 'approved')",
            (
                str(user_id),
                produto_key,
                valor,
                quantidade,
                f"manual:{admin_id}:{user_id}:{produto_key}",
            ),
        )
        return cur.lastrowid


def resumo_cliente(user_id: int) -> tuple[int, float]:
    with connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(valor), 0) AS total FROM pedidos "
            "WHERE discord_user_id=? AND status='approved'",
            (str(user_id),),
        ).fetchone()
    return int(row["n"]), float(row["total"])


def ultimas_compras(user_id: int, limite: int = 5):
    with connection() as conn:
        return conn.execute(
            "SELECT produto_key, valor, criado_em FROM pedidos WHERE discord_user_id=? AND status='approved' "
            "ORDER BY id DESC LIMIT ?",
            (str(user_id), limite),
        ).fetchall()


def top_clientes(limite: int = 10):
    with connection() as conn:
        return conn.execute(
            "SELECT discord_user_id, COUNT(*) AS n, SUM(valor) AS total FROM pedidos WHERE status='approved' "
            "GROUP BY discord_user_id ORDER BY total DESC LIMIT ?",
            (limite,),
        ).fetchall()


def marcar_reembolsado(pedido_id: int) -> bool:
    """Passa um pedido aprovado para reembolsado. False se ele não estava aprovado (nada muda)."""
    with connection() as conn:
        cur = conn.execute(
            "UPDATE pedidos SET status=? WHERE id=? AND status=?",
            (STATUS_REEMBOLSADO, pedido_id, STATUS_APROVADO),
        )
        return cur.rowcount == 1


def resumo_financeiro() -> dict:
    """Números do /dashboard. Compras aprovadas = pedidos que foram aprovados, inclusive os já reembolsados.
    Total ganho = compras aprovadas menos reembolsos."""
    with connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS compras, COALESCE(SUM(valor), 0) AS bruto, "
            "COALESCE(SUM(CASE WHEN status=? THEN valor END), 0) AS reembolsado, "
            "COALESCE(SUM(CASE WHEN status=? THEN 1 END), 0) AS reembolsos, "
            "COUNT(DISTINCT discord_user_id) AS clientes "
            "FROM pedidos WHERE status IN (?, ?)",
            (
                STATUS_REEMBOLSADO,
                STATUS_REEMBOLSADO,
                STATUS_APROVADO,
                STATUS_REEMBOLSADO,
            ),
        ).fetchone()
    bruto, reembolsado = float(row["bruto"]), float(row["reembolsado"])
    return {
        "compras": int(row["compras"]),
        "bruto": bruto,
        "reembolsos": int(row["reembolsos"]),
        "reembolsado": reembolsado,
        "ganho": bruto - reembolsado,
        "clientes": int(row["clientes"]),
    }


def attach_payment(pedido_id, payment_id):
    with connection() as conn:
        conn.execute(
            "UPDATE pedidos SET payment_id=? WHERE id=? AND status='pending'",
            (str(payment_id), pedido_id),
        )


def set_delivery(pedido_id, status):
    with connection() as conn:
        conn.execute(
            "UPDATE pedidos SET entrega_status=? WHERE id=?", (status, pedido_id)
        )


def record_delivery(pedido_id, item, status):
    with connection() as conn:
        conn.execute(
            "INSERT INTO pedido_entregas VALUES (?,?,?,?) ON CONFLICT(pedido_id,estoque_id,kind) DO UPDATE SET status=excluded.status",
            (pedido_id, item["id"], item["kind"], status),
        )


def delivered_count(pedido_id):
    with connection() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM pedido_entregas WHERE pedido_id=? AND status='sent'",
            (pedido_id,),
        ).fetchone()[0]


def uncertain_delivery(pedido_id):
    with connection() as conn:
        return bool(
            conn.execute(
                "SELECT 1 FROM pedido_entregas WHERE pedido_id=? AND status='sending'",
                (pedido_id,),
            ).fetchone()
        )


def claim_sale(pedido_id):
    with connection() as conn:
        return (
            conn.execute(
                "UPDATE pedidos SET venda_anunciada=1 WHERE id=? AND venda_anunciada=0",
                (pedido_id,),
            ).rowcount
            == 1
        )


def release_unstarted(pedido_id):
    from . import stock

    with connection() as conn:
        rows = conn.execute(
            "SELECT estoque_id,kind FROM pedido_entregas WHERE pedido_id=? AND status='reserved'",
            (pedido_id,),
        ).fetchall()
        for row in rows:
            stock.release({"id": row["estoque_id"], "kind": row["kind"]})
        conn.execute(
            "UPDATE pedido_entregas SET status='failed' WHERE pedido_id=? AND status='reserved'",
            (pedido_id,),
        )
