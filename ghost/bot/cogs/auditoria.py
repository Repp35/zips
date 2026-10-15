"""Log das ações sensíveis feitas por admin (o dono primário não entra: as ações dele não são relatadas).

Usa os eventos de sucesso do discord.py, então vale para a barra (/) e para o prefixo (>) ao mesmo tempo.
"""

import logging

import discord
from discord.ext import commands

from ...config import settings
from ...ui.emojis import E
from ...services import notify
from ...config.settings import CORES

log = logging.getLogger("auditoria")

SENSIVEIS = {
    "ban",
    "timeout",
    "lock",
    "unlock",
    "fechar",
    "aprovar",
    "anunciar",
    "setup",
}


async def _relatar(autor, nome: str, detalhe: str) -> None:
    if autor is None or autor.id == settings.ADMIN_ID or nome not in SENSIVEIS:
        return
    await notify.registrar(
        "acao_admin",
        f"{E.shield} Ação de admin",
        CORES["aviso"],
        [
            ("Comando", f"/{nome}"),
            ("Detalhe", (detalhe or "—")[:900]),
            ("Por", f"{autor} ({autor.id})"),
        ],
    )


class Auditoria(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_app_command_completion(
        self, interaction: discord.Interaction, command
    ) -> None:
        try:
            valores = dict(vars(interaction.namespace))
        except TypeError:
            valores = {}
        detalhe = ", ".join(
            f"{k}={v}" for k, v in valores.items() if v not in (None, "")
        )
        await _relatar(interaction.user, command.qualified_name, detalhe)

    @commands.Cog.listener()
    async def on_command_completion(self, ctx: commands.Context) -> None:
        await _relatar(ctx.author, ctx.command.qualified_name, ctx.message.content)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Auditoria(bot))
