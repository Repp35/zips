"""Acesso serializado ao SQLite e inicialização do schema do bot."""

import logging
import os
import sqlite3
import threading
from contextlib import contextmanager
from typing import Iterator

from ..config import settings
from .migrations import apply as apply_migrations

log = logging.getLogger("db")

_conn: sqlite3.Connection | None = None
_conn_path: str | None = None
_depth = 0
_lock = threading.RLock()


def _connect() -> sqlite3.Connection:
    """Abre/reutiliza a conexão do processo; os escopos de uso são serializados por `_lock`."""
    global _conn, _conn_path
    if _conn is not None and _conn_path != settings.DB_PATH:
        close()
    if _conn is None:
        if settings.DB_PATH != ":memory:":
            os.makedirs(os.path.dirname(settings.DB_PATH), mode=0o700, exist_ok=True)
        conn = sqlite3.connect(settings.DB_PATH, timeout=10, check_same_thread=False)
        if settings.DB_PATH != ":memory:":
            os.chmod(settings.DB_PATH, 0o600)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = FULL")
        conn.execute("PRAGMA busy_timeout = 10000")
        _conn, _conn_path = conn, settings.DB_PATH
    return _conn


def close() -> None:
    global _conn, _conn_path, _depth
    with _lock:
        if _conn is not None:
            _conn.close()
        _conn, _conn_path, _depth = None, None, 0


@contextmanager
def connection() -> Iterator[sqlite3.Connection]:
    """Serializa usos síncronos; o escopo externo confirma e os internos usam savepoint."""
    global _depth
    with _lock:
        conn = _connect()
        savepoint = f"ghost_nested_{_depth}" if _depth else None
        if savepoint:
            if not conn.in_transaction:
                conn.execute("BEGIN")
            conn.execute(f"SAVEPOINT {savepoint}")
        _depth += 1
        try:
            yield conn
        except BaseException:
            if savepoint:
                conn.execute(f"ROLLBACK TO {savepoint}")
                conn.execute(f"RELEASE {savepoint}")
            else:
                conn.rollback()
            raise
        else:
            if savepoint:
                conn.execute(f"RELEASE {savepoint}")
            if _depth == 1:
                try:
                    conn.commit()
                except BaseException:
                    conn.rollback()
                    raise
        finally:
            _depth -= 1


def init_db() -> None:
    """Aplica migrações numa transação atômica, também para um banco vazio."""
    with _lock:
        conn = _connect()
        if conn.in_transaction:
            raise RuntimeError("não é possível inicializar o banco dentro de uma transação")
        conn.execute("PRAGMA foreign_keys = OFF")
        try:
            conn.execute("BEGIN IMMEDIATE")
            apply_migrations(conn)
            violations = conn.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise sqlite3.IntegrityError(
                    f"migração gerou {len(violations)} referência(s) estrangeira(s) inválida(s)"
                )
            conn.commit()
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.execute("PRAGMA foreign_keys = ON")
    log.info("banco pronto: %s", settings.DB_PATH)
