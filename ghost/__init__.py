"""Pacote principal do bot."""

import importlib
import sys

# Aliases de compatibilidade para extensões e instalações antigas.
from .config import settings
from .core import cache, diario, eventos
from .database import connection as _connection, init_db as _init_db
from .database.connection import close as _close_db

_database_module = importlib.import_module(".database.connection", __name__)

sys.modules.setdefault(__name__ + ".db", _database_module)


class _DatabaseCompat:
    connection = staticmethod(_connection)
    init_db = staticmethod(_init_db)
    close = staticmethod(_close_db)


db = _DatabaseCompat()

__all__ = ["settings", "cache", "diario", "eventos", "db"]
