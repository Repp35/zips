"""Cargo da equipe: quem é admin no bot recebe o cargo de equipe no servidor, e perde quando sai da equipe."""

import logging

import discord

from . import papeis

log = logging.getLogger("equipe")


async def aplicar(guild: discord.Guild | None, user_id: int, add: bool) -> bool:
    if guild is None:
        return False
    cargo = papeis.fixo(guild, "equipe")
    if cargo is None:
        return False
    try:
        membro = guild.get_member(user_id) or await guild.fetch_member(user_id)
    except discord.HTTPException:
        return False
    try:
        if add:
            await membro.add_roles(cargo, reason="Equipe do bot")
        else:
            await membro.remove_roles(cargo, reason="Saiu da equipe do bot")
        return True
    except discord.HTTPException as exc:
        log.warning(
            "não consegui %s o cargo de equipe em %s: %s",
            "dar" if add else "tirar",
            user_id,
            exc,
        )
        return False
