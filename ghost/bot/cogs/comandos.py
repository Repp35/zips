"""Comandos de barra e de prefixo (>) da equipe: emoji, setup, fechar, aprovar, perfil, clear, lock e unlock.

Aqui fica só a entrada (barra ou prefixo), a permissão e o envio da resposta. A regra de cada comando está em
cogs/comandos_acoes.py, então os dois jeitos de chamar produzem o mesmo resultado.
"""

import logging

import discord
from discord import app_commands
from discord.ext import commands

from ... import catalogo
from ...ui import layout
from ...data import stock
from ...ui.emojis import E
from ...services import rest
from ...config.settings import CORES
from ..checks import admin_only, staff_only
from ..router import route
from . import comandos_acoes as acoes

log = logging.getLogger("comandos")

VITRINES = [
    app_commands.Choice(name=v.nome_setup, value=v.id) for v in catalogo.vitrines()
]
ACOES_SETUP = [
    app_commands.Choice(name="add", value="add"),
    app_commands.Choice(name="remove", value="remove"),
    app_commands.Choice(name="atualizar", value="atualizar"),
    app_commands.Choice(name="Configurar canal do agente", value="agente"),
]
ACOES_EMOJI = [
    app_commands.Choice(name="inspecionar", value="inspecionar"),
    app_commands.Choice(name="add", value="add"),
    app_commands.Choice(name="remover", value="remover"),
]


async def produto_autocomplete(
    interaction: discord.Interaction, atual: str
) -> "list[app_commands.Choice[str]]":
    """Só produtos de entrega automática: a entrega manual de /aprovar usa o estoque deles."""
    termo = atual.lower()
    produtos = [
        p
        for p in catalogo.produtos(so_ativos=False)
        if p.modelo.entrega == "automatica"
        and (termo in p.nome.lower() or termo in p.id)
    ]
    return [
        app_commands.Choice(
            name=f"{p.nome} (estoque {stock.count_global(p.id)})"[:100], value=p.id
        )
        for p in produtos[:25]
    ]


def _cartao(titulo: str, texto: str, efemero: bool) -> dict:
    return layout.mensagem(
        layout.cartao(layout.titulo(titulo), layout.texto(texto), cor=CORES["neutro"]),
        efemera=efemero,
    )


def _painel_emoji(guild: discord.Guild, efemero: bool) -> dict:
    opcoes = [
        {
            "label": e.name[:100],
            "value": str(e.id),
            "emoji": {"name": e.name, "id": str(e.id), "animated": e.animated},
        }
        for e in guild.emojis[:25]
    ]
    if not opcoes:
        return _cartao(
            f"{E.information} Emojis", "Este servidor ainda não tem emojis.", efemero
        )
    partes = [
        layout.titulo(f"{E.information} Inspecionar emoji"),
        layout.texto("Escolha um emoji para ver as informações."),
        layout.linha(layout.seletor("emo:ver", "Selecione um emoji...", opcoes)),
    ]
    if len(guild.emojis) > 25:
        partes.append(
            layout.rodape(
                f"Mostrando 25 de {len(guild.emojis)}. "
                "Para os outros, use `/emoji emoji:<emoji>`."
            )
        )
    return layout.mensagem(layout.cartao(*partes, cor=CORES["neutro"]), efemera=efemero)


async def _responder_seguro(ctx: commands.Context, texto: str) -> None:
    """Responde no canal. Pode falhar quando o canal acabou de ser apagado (ex.: /fechar)."""
    try:
        await ctx.reply(texto, mention_author=False)
    except discord.HTTPException:
        pass


class Comandos(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_command_error(
        self, ctx: commands.Context, error: commands.CommandError
    ) -> None:
        if isinstance(error, commands.CheckFailure):
            return
        if isinstance(error, (commands.BadArgument, commands.MissingRequiredArgument)):
            await ctx.reply(
                f"{E.wrong} Não entendi os argumentos. Confira o formato do comando.",
                mention_author=False,
            )
            return
        log.error("erro no comando %s", ctx.command, exc_info=error)
        await ctx.reply(
            f"{E.wrong} Não consegui executar. Veja os logs.", mention_author=False
        )

    # ── /emoji ────────────────────────────────────────────────────────────────

    @app_commands.command(name="emoji", description="[ admin ]")
    @app_commands.describe(
        acao="Inspecionar (padrão), adicionar ou remover",
        emoji="Emoji colado, ex.: um de uma mensagem",
        nome="Nome do emoji",
        url="Link https da imagem (para adicionar)",
        imagem="Imagem anexada (para adicionar, no lugar do link)",
    )
    @app_commands.choices(acao=ACOES_EMOJI)
    @staff_only()
    async def slash_emoji(
        self,
        interaction: discord.Interaction,
        acao: str,
        emoji: str | None = None,
        nome: str | None = None,
        url: str | None = None,
        imagem: discord.Attachment | None = None,
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                f"{E.wrong} Use dentro do servidor.", ephemeral=True
            )
            return
        if acao == "inspecionar":
            alvo = emoji or nome
            if not alvo:
                await rest.responder(
                    interaction, _painel_emoji(interaction.guild, True)
                )
                return
            achado = acoes.achar_emoji(interaction.guild, alvo)
            if achado is None:
                await interaction.response.send_message(
                    f"{E.wrong} Não achei `{alvo}` neste servidor.", ephemeral=True
                )
                return
            await interaction.response.defer(ephemeral=True)
            texto, arquivo = await acoes.inspecionar(interaction.guild, *achado)
            await interaction.followup.send(
                texto, ephemeral=True, **({"file": arquivo} if arquivo else {})
            )
            return
        await interaction.response.defer(ephemeral=True)
        if acao == "add":
            texto = await acoes.adicionar_emoji(
                interaction.guild, interaction.user, nome, url, imagem
            )
        else:
            texto = await acoes.remover_emoji(
                interaction.guild, interaction.user, nome or emoji or ""
            )
        await interaction.followup.send(texto, ephemeral=True)

    @commands.group(name="emoji", invoke_without_command=True)
    @acoes.exige("admin")
    async def pref_emoji(self, ctx: commands.Context, emoji: str | None = None) -> None:
        """>emoji — lista para inspecionar; >emoji <emoji colado> — ficha direta."""
        if ctx.guild is None:
            await ctx.reply(f"{E.wrong} Use dentro do servidor.", mention_author=False)
            return
        if emoji is None:
            await rest.send_message(ctx.channel.id, _painel_emoji(ctx.guild, False))
            return
        achado = acoes.achar_emoji(ctx.guild, emoji)
        if achado is None:
            await ctx.reply(
                f"{E.wrong} Não achei `{emoji}` neste servidor.", mention_author=False
            )
            return
        texto, arquivo = await acoes.inspecionar(ctx.guild, *achado)
        await ctx.reply(
            texto, mention_author=False, **({"file": arquivo} if arquivo else {})
        )

    @pref_emoji.command(name="add")
    @acoes.exige("admin")
    async def pref_emoji_add(
        self, ctx: commands.Context, nome: str, url: str | None = None
    ) -> None:
        """>emoji add <nome> <url|anexo>"""
        anexo = ctx.message.attachments[0] if ctx.message.attachments else None
        texto = await acoes.adicionar_emoji(ctx.guild, ctx.author, nome, url, anexo)
        await ctx.reply(texto, mention_author=False)

    @pref_emoji.command(name="remove")
    @acoes.exige("admin")
    async def pref_emoji_remove(self, ctx: commands.Context, alvo: str) -> None:
        """>emoji remove <nome|id>"""
        await ctx.reply(
            await acoes.remover_emoji(ctx.guild, ctx.author, alvo), mention_author=False
        )

    # ── /setup ────────────────────────────────────────────────────────────────

    @app_commands.command(name="setup", description="[ admin ]")
    @app_commands.describe(
        acao="Gerenciar vitrines ou configurar o canal do agente",
        vitrine="Vitrine do catálogo",
        canal="Canal da vitrine ou canal do agente (padrão: este canal)",
        ids="IDs de produto separados por vírgula, ou todos (para atualizar)",
    )
    @app_commands.choices(acao=ACOES_SETUP, vitrine=VITRINES)
    @admin_only()
    async def slash_setup(
        self,
        interaction: discord.Interaction,
        acao: str,
        vitrine: str | None = None,
        canal: discord.TextChannel | None = None,
        ids: str | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        if acao == "agente":
            if interaction.guild is None:
                await interaction.followup.send(
                    f"{E.wrong} Use dentro do servidor.", ephemeral=True
                )
                return
            destino = canal or interaction.channel
            if not isinstance(
                destino,
                (discord.TextChannel, discord.VoiceChannel, discord.Thread),
            ):
                await interaction.followup.send(
                    f"{E.wrong} Escolha um canal de texto, voz ou tópico válido.",
                    ephemeral=True,
                )
                return
            from .agente import configurar_canal

            texto = await configurar_canal(destino.id, interaction.user)
            await interaction.followup.send(texto, ephemeral=True)
            return
        linhas = await acoes.executar_setup(
            acao, vitrine, canal or interaction.channel, ids, interaction.user
        )
        await interaction.followup.send(acoes.juntar(linhas), ephemeral=True)

    @commands.group(name="setup", invoke_without_command=True)
    @acoes.exige("admin")
    async def pref_setup(self, ctx: commands.Context) -> None:
        await ctx.reply(
            f"{E.information} Use `>setup add <vitrine> [canal]`, `>setup remove <vitrine>` "
            "ou `>setup atualizar <ids|todos>`.",
            mention_author=False,
        )

    @pref_setup.command(name="add")
    @acoes.exige("admin")
    async def pref_setup_add(
        self,
        ctx: commands.Context,
        vitrine: str,
        canal: discord.TextChannel | None = None,
    ) -> None:
        linhas = await acoes.executar_setup(
            "add", vitrine, canal or ctx.channel, None, ctx.author
        )
        await ctx.reply(acoes.juntar(linhas), mention_author=False)

    @pref_setup.command(name="remove")
    @acoes.exige("admin")
    async def pref_setup_remove(self, ctx: commands.Context, vitrine: str) -> None:
        linhas = await acoes.executar_setup("remove", vitrine, None, None, ctx.author)
        await ctx.reply(acoes.juntar(linhas), mention_author=False)

    @pref_setup.command(name="atualizar")
    @acoes.exige("admin")
    async def pref_setup_atualizar(self, ctx: commands.Context, *, ids: str) -> None:
        linhas = await acoes.executar_setup("atualizar", None, None, ids, ctx.author)
        await ctx.reply(acoes.juntar(linhas), mention_author=False)

    # ── /fechar ───────────────────────────────────────────────────────────────

    @app_commands.command(name="fechar", description="[ admin ]")
    @admin_only()
    async def slash_fechar(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        texto = await acoes.fechar(interaction.channel, interaction.user)
        await interaction.followup.send(texto, ephemeral=True)

    @commands.command(name="fechar")
    @acoes.exige("admin")
    async def pref_fechar(self, ctx: commands.Context) -> None:
        await _responder_seguro(ctx, await acoes.fechar(ctx.channel, ctx.author))

    # ── /aprovar ──────────────────────────────────────────────────────────────

    @app_commands.command(name="aprovar", description="[ admin ]")
    @app_commands.describe(
        usuario="Cliente (só para entrega manual)",
        produto="Produto com entrega automática (só para entrega manual)",
        quantidade="Quantos itens, de 1 a 10",
        gratis="Entregar sem cobrar",
    )
    @app_commands.autocomplete(produto=produto_autocomplete)
    @admin_only()
    async def slash_aprovar(
        self,
        interaction: discord.Interaction,
        usuario: discord.User | None = None,
        produto: str | None = None,
        quantidade: app_commands.Range[int, 1, 10] = 1,
        gratis: bool = False,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        texto = await acoes.aprovar(
            interaction.channel,
            interaction.user,
            interaction.guild,
            usuario,
            produto,
            quantidade,
            gratis,
        )
        await interaction.followup.send(texto, ephemeral=True)

    @commands.command(name="aprovar")
    @acoes.exige("admin")
    async def pref_aprovar(
        self,
        ctx: commands.Context,
        usuario: discord.User | None = None,
        produto: str | None = None,
        quantidade: int = 1,
        gratis: bool = False,
    ) -> None:
        """>aprovar — na thread do pedido; >aprovar @usuario produto [quantidade] [gratis] — entrega manual."""
        texto = await acoes.aprovar(
            ctx.channel, ctx.author, ctx.guild, usuario, produto, quantidade, gratis
        )
        await _responder_seguro(ctx, texto)

    # ── /perfil ───────────────────────────────────────────────────────────────

    @app_commands.command(name="perfil", description="[ admin ]")
    @app_commands.describe(usuario="Cliente (sem ele, mostra o ranking)")
    @staff_only()
    async def slash_perfil(
        self, interaction: discord.Interaction, usuario: discord.User | None = None
    ) -> None:
        titulo, texto = acoes.perfil(usuario)
        await rest.responder(interaction, _cartao(titulo, texto, True))

    @commands.command(name="perfil")
    @acoes.exige("admin")
    async def pref_perfil(
        self, ctx: commands.Context, usuario: discord.User | None = None
    ) -> None:
        """>perfil — ranking; >perfil @usuario|id — compras do cliente."""
        titulo, texto = acoes.perfil(usuario)
        await rest.send_message(ctx.channel.id, _cartao(titulo, texto, False))

    # ── /clear ────────────────────────────────────────────────────────────────

    @app_commands.command(name="clear", description="[ admin ]")
    @app_commands.describe(quantidade="Quantas mensagens apagar, de 1 a 1000")
    @staff_only()
    async def slash_clear(
        self,
        interaction: discord.Interaction,
        quantidade: str,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        if quantidade.lower() == "all":
            limite = 1000
        else:
            try:
                limite = int(quantidade)
            except ValueError:
                limite = 0
        if not 1 <= limite <= 1000:
            await interaction.followup.send(
                f"{E.wrong} Use um número de 1 a 1000 ou `all`.", ephemeral=True
            )
            return
        await interaction.followup.send(
            await acoes.limpar(interaction.channel, limite), ephemeral=True
        )

    @commands.command(name="clear")
    @acoes.exige("admin")
    async def pref_clear(self, ctx: commands.Context, quantidade: str) -> None:
        """>clear N — apaga N mensagens; >clear all apaga até 1000 antes deste comando."""
        if quantidade.lower() == "all":
            limite = 1000
        else:
            try:
                limite = int(quantidade)
            except ValueError:
                limite = 0
        if not 1 <= limite <= 1000:
            await ctx.reply(
                f"{E.wrong} Use um número de 1 a 1000 ou `all`.", mention_author=False
            )
            return
        texto = await acoes.limpar(ctx.channel, limite, antes=ctx.message)
        try:
            await ctx.message.delete()
        except discord.HTTPException:
            pass
        await ctx.send(texto, delete_after=5)

    # ── /lock e /unlock (admin) ─────────────────────────────────────

    @app_commands.command(name="lock", description="[ admin ]")
    @admin_only()
    async def slash_lock(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        texto = await acoes.travar(
            interaction.channel, interaction.guild, interaction.user
        )
        await interaction.followup.send(texto, ephemeral=True)

    @commands.command(name="lock")
    @acoes.exige("admin")
    async def pref_lock(self, ctx: commands.Context) -> None:
        await _responder_seguro(
            ctx, await acoes.travar(ctx.channel, ctx.guild, ctx.author)
        )

    @app_commands.command(name="unlock", description="[ admin ]")
    @admin_only()
    async def slash_unlock(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        texto = await acoes.destravar(
            interaction.channel, interaction.guild, interaction.user
        )
        await interaction.followup.send(texto, ephemeral=True)

    @commands.command(name="unlock")
    @acoes.exige("admin")
    async def pref_unlock(self, ctx: commands.Context) -> None:
        await _responder_seguro(
            ctx, await acoes.destravar(ctx.channel, ctx.guild, ctx.author)
        )

    # ── Componentes do /emoji ─────────────────────────────────────────────────

    @route(prefix="emo:ver")
    async def _ver_emoji(self, interaction: discord.Interaction, cid: str) -> None:
        if not acoes.pode(interaction.user.id, "staff"):
            await interaction.response.send_message(
                f"{E.wrong} Sem permissão.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        texto, arquivo = await acoes.inspecionar(
            interaction.guild, int(interaction.data["values"][0])
        )
        await interaction.followup.send(
            texto, ephemeral=True, **({"file": arquivo} if arquivo else {})
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Comandos(bot))
