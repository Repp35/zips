"""Locks por recurso; entradas ociosas são coletadas automaticamente."""

import asyncio
from weakref import WeakValueDictionary

_locks = WeakValueDictionary()


def resource(key: str) -> asyncio.Lock:
    lock = _locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _locks[key] = lock
    return lock
