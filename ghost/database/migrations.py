"""Migrações versionadas e atômicas do schema SQLite."""

import logging
import sqlite3

from .schema import INDEXES, SCHEMA, SCHEMA_VERSION

log = logging.getLogger("db.migrations")

COLUMN_MIGRATIONS = {
    "canais_snapshot": {"permissoes": "TEXT"},
    "estoque": {"extra": "TEXT"},
    "estoque_links": {"extra": "TEXT"},
    "pedidos": {
        "checkout_descricao": "TEXT",
        "checkout_email": "TEXT",
        "checkout_key": "TEXT",
        "entrega_status": "TEXT NOT NULL DEFAULT 'legacy'",
        "pix_publicado": "INTEGER NOT NULL DEFAULT 0",
        "quantidade": "INTEGER NOT NULL DEFAULT 1",
        "venda_anunciada": "INTEGER NOT NULL DEFAULT 0",
    },
    "threads_ativas": {
        "assumido_por": "TEXT",
        "entrega_manual": "INTEGER NOT NULL DEFAULT 0",
        "quantidade": "INTEGER NOT NULL DEFAULT 1",
        "ticket_escolha": "TEXT",
    },
}


def apply(conn: sqlite3.Connection) -> int:
    """Aplica versões pendentes; o chamador controla a transação atômica."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations "
        "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    row = conn.execute("SELECT MAX(version) AS version FROM schema_migrations").fetchone()
    version = int(row["version"] or 0)
    if version > SCHEMA_VERSION:
        raise RuntimeError(
            f"banco na versão {version}, mas este bot suporta até {SCHEMA_VERSION}"
        )
    if version < 1:
        _execute_script(conn, SCHEMA)
        _migrate_columns(conn)
        _migrate_coupons(conn)
        _execute_script(conn, INDEXES)
        _migrate_notices(conn)
        conn.execute("INSERT INTO schema_migrations(version) VALUES (1)")
        version = 1
    return version


def _execute_script(conn: sqlite3.Connection, script: str) -> None:
    """Executa DDL sem `executescript`, que faria commit implícito."""
    statement = ""
    for line in script.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            if statement.strip():
                conn.execute(statement)
            statement = ""
    if statement.strip():
        raise sqlite3.OperationalError("script SQL terminou com uma instrução incompleta")


def _existing_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _migrate_columns(conn: sqlite3.Connection) -> None:
    for table, columns in COLUMN_MIGRATIONS.items():
        existing = _existing_columns(conn, table)
        for name, declaration in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
                log.info("coluna adicionada: %s.%s", table, name)

    for table in ("estoque", "estoque_links"):
        existing = _existing_columns(conn, table)
        for name, declaration in (
            ("reservado", "INTEGER NOT NULL DEFAULT 0"),
            ("reservado_em", "TEXT"),
            ("reservado_por", "TEXT"),
        ):
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
                log.info("coluna adicionada: %s.%s", table, name)

    coupon_columns = {
        "desconto_valor": "REAL",
        "tipo_desconto": "TEXT NOT NULL DEFAULT 'pct'",
        "min_compras": "INTEGER NOT NULL DEFAULT 0",
        "limite_por_usuario": "INTEGER",
        "cargos_permitidos": "TEXT",
        "cargos_bloqueados": "TEXT",
        "usuarios_permitidos": "TEXT",
        "usuarios_bloqueados": "TEXT",
    }
    existing = _existing_columns(conn, "cupons")
    for name, declaration in coupon_columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE cupons ADD COLUMN {name} {declaration}")
            log.info("coluna adicionada: cupons.%s", name)


def _migrate_coupons(conn: sqlite3.Connection) -> None:
    """Substitui a restrição antiga que impedia cupons de desconto em valor."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='cupons'"
    ).fetchone()
    if row is None or "desconto_pct INTEGER NOT NULL" not in (row["sql"] or ""):
        return
    conn.execute(
        """
        CREATE TABLE cupons_novo (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nome TEXT NOT NULL UNIQUE COLLATE NOCASE,
            desconto_pct INTEGER CHECK (desconto_pct IS NULL OR desconto_pct BETWEEN 1 AND 99),
            desconto_valor REAL CHECK (desconto_valor IS NULL OR desconto_valor > 0),
            tipo_desconto TEXT NOT NULL DEFAULT 'pct' CHECK (tipo_desconto IN ('pct', 'valor')),
            primeira_compra INTEGER NOT NULL DEFAULT 0,
            min_produto REAL NOT NULL DEFAULT 0.0,
            min_compras INTEGER NOT NULL DEFAULT 0,
            limite_usos INTEGER,
            limite_por_usuario INTEGER,
            usos_atuais INTEGER NOT NULL DEFAULT 0,
            validade TEXT,
            ativo INTEGER NOT NULL DEFAULT 1,
            cargos_permitidos TEXT,
            cargos_bloqueados TEXT,
            usuarios_permitidos TEXT,
            usuarios_bloqueados TEXT,
            criado_em TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    destination = [row["name"] for row in conn.execute("PRAGMA table_info(cupons_novo)")]
    source = _existing_columns(conn, "cupons")
    columns = [name for name in destination if name in source]
    joined = ", ".join(columns)
    conn.execute(f"INSERT INTO cupons_novo ({joined}) SELECT {joined} FROM cupons")
    conn.execute("DROP TABLE cupons")
    conn.execute("ALTER TABLE cupons_novo RENAME TO cupons")


def _migrate_notices(conn: sqlite3.Connection) -> None:
    """Migra inscrições antigas de restock uma única vez."""
    if conn.execute("SELECT 1 FROM config WHERE chave='migracao_avisos'").fetchone():
        return
    conn.execute(
        "INSERT OR IGNORE INTO avisos_estoque (user_id, produto_key) "
        "SELECT user_id, produto_key FROM restock_interessados WHERE preferencia='notificar'"
    )
    conn.execute("INSERT INTO config (chave, valor) VALUES ('migracao_avisos', '1')")
