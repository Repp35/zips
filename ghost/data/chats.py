"""Chats travados pelo /lock: guarda a permissão original de @everyone, para o /unlock devolver exatamente igual."""

from ..database.connection import connection


def guardar(
    canal_id: int, tinha_regra: bool, permite: int, nega: int, por: str, quando: str
) -> None:
    with connection() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO chats_travados (canal_id, tinha_regra, permite, nega, travado_por, "
            "travado_em) VALUES (?,?,?,?,?,?)",
            (str(canal_id), int(tinha_regra), permite, nega, por, quando),
        )


def pegar(canal_id: int) -> dict | None:
    with connection() as conn:
        row = conn.execute(
            "SELECT * FROM chats_travados WHERE canal_id=?", (str(canal_id),)
        ).fetchone()
    return dict(row) if row else None


def apagar(canal_id: int) -> None:
    with connection() as conn:
        conn.execute("DELETE FROM chats_travados WHERE canal_id=?", (str(canal_id),))
