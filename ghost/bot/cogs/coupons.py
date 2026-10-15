"""Painel de cupons para a equipe. Aplicação dos cupons acontece no carrinho (ver cogs/store)."""

import discord
from discord import app_commands
from discord.ext import commands

from ..checks import staff_only
from ...ui.emojis import E


class Coupons(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="cupom", description="[ admin ]")
    @staff_only()
    async def cupom(self, interaction: discord.Interaction) -> None:
        embed = discord.Embed(title=f"{E.ticket} Gerenciar Cupons", color=0x2B2D31)
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Coupons(bot))
