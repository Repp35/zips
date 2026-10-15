"""Cache em memória com expiração por tempo."""

import time
from typing import Any


class TTLCache:
    def __init__(self, ttl_seconds: float):
        self.ttl = ttl_seconds
        self._items: dict[str, tuple[Any, float]] = {}

    def get(self, key: str) -> Any | None:
        item = self._items.get(key)
        if item is None:
            return None
        value, expires = item
        if time.monotonic() >= expires:
            self._items.pop(key, None)
            return None
        return value

    def set(self, key: str, value: Any) -> None:
        self._items[key] = (value, time.monotonic() + self.ttl)

    def clear(self) -> None:
        self._items.clear()


estoque_cache = TTLCache(60)
equipe_cache = TTLCache(float("inf"))  # válido até set_team_member limpar
