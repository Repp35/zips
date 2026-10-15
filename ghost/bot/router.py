"""Despacho de componentes e modais pelo custom_id, com rotas declaradas nas cogs."""

import logging
from typing import Awaitable, Callable

import discord
from ..ui.emojis import E

log = logging.getLogger("router")

Handler = Callable[[discord.Interaction, str], Awaitable[None]]


def route(*, exact: str | None = None, prefix: str | None = None):
    """Marca um método de cog como handler de um custom_id exato ou por prefixo."""

    def decorator(fn):
        fn.__routes__ = [*getattr(fn, "__routes__", []), (exact, prefix)]
        return fn

    return decorator


class Router:
    def __init__(self) -> None:
        self._exact: dict[str, Handler] = {}
        self._prefix: list[tuple[str, Handler]] = []

    def add(self, exact: str | None, prefix: str | None, handler: Handler) -> None:
        if exact:
            self._exact[exact] = handler
        if prefix:
            self._prefix.append((prefix, handler))
            self._prefix.sort(key=lambda item: len(item[0]), reverse=True)

    def match(self, cid: str) -> Handler | None:
        if cid in self._exact:
            return self._exact[cid]
        for prefix, handler in self._prefix:
            if cid.startswith(prefix):
                return handler
        return None

    async def dispatch(self, interaction: discord.Interaction) -> bool:
        cid = (interaction.data or {}).get("custom_id", "")
        handler = self.match(cid) if cid else None
        if handler is None and cid:
            from .. import catalogo

            for v in catalogo.vitrines():
                selector = v.dados.get("seletor", {})
                if v.tipo in ("selecao", "painel") and cid in {
                    v.dados.get("custom_id"),
                    selector.get("custom_id"),
                }:
                    handler = self.match("select_produto_")
                    break
        if handler is None:
            return False
        try:
            await handler(interaction, cid)
        except Exception as exc:
            log.exception("erro no handler de %s", cid)
            await _reply_error(interaction, exc)
        return True


async def _reply_error(interaction: discord.Interaction, exc: Exception) -> None:
    log.error("erro no handler de componente", exc_info=exc)
    texto = f"{E.wrong} Ocorreu um erro inesperado. Tente novamente ou abra um ticket de suporte."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(texto, ephemeral=True)
        else:
            await interaction.response.send_message(texto, ephemeral=True)
    except discord.HTTPException:
        pass
