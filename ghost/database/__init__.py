"""Infraestrutura persistente do bot."""

from .connection import connection, init_db

__all__ = ["connection", "init_db"]
