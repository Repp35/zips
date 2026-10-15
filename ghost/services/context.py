"""Referência ao bot em execução, usada pelos serviços que precisam buscar canais e usuários."""

import discord

_bot: discord.Client | None = None


def set_bot(bot: discord.Client) -> None:
    global _bot
    _bot = bot


def bot() -> discord.Client:
    if _bot is None:
        raise RuntimeError("bot ainda não iniciado")
    return _bot


async def channel(channel_id: int):
    client = bot()
    return client.get_channel(channel_id) or await client.fetch_channel(channel_id)


async def user(user_id: int) -> discord.User:
    client = bot()
    return client.get_user(user_id) or await client.fetch_user(user_id)
