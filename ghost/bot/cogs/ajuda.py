"""/help e >help: lista de todos os comandos com o nível de cada um. O conteúdo vem de ghost/ajuda/help.json.

O dono pode trocar a imagem desta ajuda: /help imagem:<link>, /help anexo:<arquivo> ou >help imagem <link>
(ou >help imagem com a imagem anexada na mensagem). Sem nada disso, mostra a ajuda.
"""

import discord
from discord import app_commands
from discord.ext import commands

from ... import ajuda
from ...config import settings
from ...data import store
from ...ui.emojis import E
from ...config.settings import CORES
from ...services import notify
from ..checks import admin_only


async def _trocar_imagem(autor: discord.abc.User, url: str) -> str:
    """Só o dono troca a imagem. Devolve a mensagem para quem pediu."""
    if autor.id != settings.ADMIN_ID:
        return f"{E.wrong} Só o dono pode trocar a imagem do help."
    try:
        ajuda.definir_imagem("geral", url)
    except ValueError as exc:
        return f"{E.wrong} {exc}."
    await notify.registrar(
        "ajuda_imagem_alterada",
        f"{E.information} Imagem do help alterada",
        CORES["info"],
        [
            ("Help", "geral"),
            ("Imagem", url.strip() or "removida"),
            ("Por", f"{autor} ({autor.id})"),
        ],
    )
    return (
        f"{E.correct} Imagem do /help salva."
        if url.strip()
        else f"{E.correct} Imagem do /help removida."
    )


class Ajuda(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="help",
        description="[ admin ]",
    )
    @app_commands.describe(
        imagem="Só o dono: link https:// da imagem",
        anexo="Só o dono: arquivo de imagem (alternativa ao link)",
    )
    @admin_only()
    async def help_cmd(
        self,
        interaction: discord.Interaction,
        imagem: str | None = None,
        anexo: discord.Attachment | None = None,
    ) -> None:
        if anexo is not None or imagem is not None:
            url = imagem or ""
            if anexo is not None:
                if not (anexo.content_type or "").startswith("image/"):
                    await interaction.response.send_message(
                        f"{E.wrong} O anexo precisa ser uma imagem.", ephemeral=True
                    )
                    return
                url = anexo.url
            await interaction.response.send_message(
                await _trocar_imagem(interaction.user, url), ephemeral=True
            )
            return
        await ajuda.enviar_interacao(interaction, ajuda.mensagens_help(efemera=False))

    @commands.command(name="help")
    async def help_prefixo(self, ctx: commands.Context, *, pedido: str = "") -> None:
        if not store.is_admin(ctx.author.id):
            return
        partes = pedido.strip().split(maxsplit=1)
        if partes and partes[0].lower() == "imagem":
            url = partes[1] if len(partes) > 1 else ""
            anexos = [
                a
                for a in ctx.message.attachments
                if (a.content_type or "").startswith("image/")
            ]
            if anexos:
                url = anexos[0].url
            await ctx.reply(await _trocar_imagem(ctx.author, url), mention_author=False)
            return
        await ajuda.enviar_canal(ctx.channel.id, ajuda.mensagens_help(efemera=False))


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Ajuda(bot))
