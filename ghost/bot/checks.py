"""Permissões dos comandos de barra: dono e admin (staff é admin)."""

import discord
from discord import app_commands

from ..data import store
from ..ui.emojis import E


def _requer(predicate, mensagem: str | None = None):
    async def check(interaction: discord.Interaction) -> bool:
        if predicate(interaction.user.id):
            return True
        texto = mensagem or f"{E.wrong} Sem permissão."
        await interaction.response.send_message(texto, ephemeral=True)
        return False

    return app_commands.check(check)


def admin_only():
    return _requer(store.is_admin)


def staff_only():
    return _requer(lambda uid: store.is_admin(uid) or store.is_staff(uid))
