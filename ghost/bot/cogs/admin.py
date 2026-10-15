"""Comandos de administração: estoque, equipe, configuração, vitrines e emojis."""

import discord
from discord import app_commands
from discord.ext import commands

from ... import catalogo
from ...core import eventos
from ...ui.components import static_view
from ...data import stock, store
from ...services import avisos, notify, shelves
from ..checks import admin_only, staff_only
from ..router import route
from ...ui.emojis import E


class ConfigModal(discord.ui.Modal):
    valor = discord.ui.TextInput(label="Novo valor", required=True)

    def __init__(self, chave: str):
        super().__init__(title=f"Configurar: {chave}")
        self.chave = chave

    async def on_submit(self, interaction: discord.Interaction) -> None:
        store.set_config(self.chave, self.valor.value.strip())
        await notify.registrar(
            "config_alterada",
            f"{E.settings} Configuração alterada",
            0xB0D4F1,
            [
                ("Chave", self.chave),
                ("Novo valor", self.valor.value.strip()),
                ("Por", f"{interaction.user} ({interaction.user.id})"),
            ],
        )
        await interaction.response.send_message(
            f"{E.correct} Configuração `{self.chave}` atualizada!", ephemeral=True
        )


class TeamModal(discord.ui.Modal):
    user_id = discord.ui.TextInput(
        label="ID do usuário", placeholder="1234567890", max_length=20
    )

    def __init__(self, tipo: str, add: bool):
        super().__init__(title=f"{'Adicionar' if add else 'Remover'} {tipo.title()}")
        self.tipo, self.add = tipo, add

    async def on_submit(self, interaction: discord.Interaction) -> None:
        uid = self.user_id.value.strip()
        store.set_team_member(self.tipo, uid, interaction.user.id, self.add)
        await notify.registrar(
            "equipe_alterada",
            f"{E.settings} Equipe alterada",
            0xB0D4F1,
            [
                ("Ação", "adicionado" if self.add else "removido"),
                ("Tipo", self.tipo),
                ("Usuário", uid),
                ("Por", f"{interaction.user} ({interaction.user.id})"),
            ],
        )
        acao = "adicionado" if self.add else "removido"
        await interaction.response.send_message(
            f"{E.correct} Usuário `{uid}` {acao} como {self.tipo}!", ephemeral=True
        )


class AddStockModal(discord.ui.Modal):
    def __init__(self, produto_key: str):
        super().__init__(title="Adicionar ao estoque")
        self.produto_key = produto_key
        is_link = stock.is_link(produto_key)
        self.entrada = discord.ui.TextInput(
            label="Links (um por linha)" if is_link else "Contas (login:senha)",
            style=discord.TextStyle.paragraph,
        )
        self.add_item(self.entrada)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        linhas = [
            linha.strip() for linha in self.entrada.value.splitlines() if linha.strip()
        ]
        estava_zerado = stock.count_global(self.produto_key) == 0
        if stock.is_link(self.produto_key):
            added = stock.add_links(self.produto_key, linhas)
        else:
            added = stock.add_accounts(self.produto_key, linhas)
        await interaction.response.send_message(
            f"{E.correct} `{added}` item(ns) adicionado(s) — {store.produto_nome(self.produto_key)}",
            ephemeral=True,
        )
        await shelves.refresh_for_product(self.produto_key)
        await notify.registrar(
            "estoque_adicionado",
            f"{E.cardbox} Estoque adicionado",
            0xB0D4F1,
            [
                ("Produto", store.produto_nome(self.produto_key)),
                ("Itens", added),
                ("Por", f"{interaction.user} ({interaction.user.id})"),
            ],
        )
        if added and estava_zerado:
            await avisos.estoque_reposto(self.produto_key, added)


class RemoveStockModal(discord.ui.Modal):
    identificador = discord.ui.TextInput(label="E-mail ou link para remover")

    def __init__(self, produto_key: str):
        super().__init__(title="Remover do estoque")
        self.produto_key = produto_key

    async def on_submit(self, interaction: discord.Interaction) -> None:
        stock.remove(self.produto_key, self.identificador.value.strip())
        await notify.registrar(
            "estoque_removido",
            f"{E.cardbox} Item removido do estoque",
            0xFFAA00,
            [
                ("Produto", store.produto_nome(self.produto_key)),
                ("Por", f"{interaction.user} ({interaction.user.id})"),
            ],
        )
        await interaction.response.send_message(
            f"{E.correct} Item removido de `{self.produto_key}`!", ephemeral=True
        )
        await shelves.refresh_for_product(self.produto_key)


def _product_select(custom_id: str, placeholder: str) -> discord.ui.View:
    options = [
        discord.SelectOption(label=p.nome, value=p.id) for p in catalogo.produtos()
    ]
    view = static_view()
    view.add_item(
        discord.ui.Select(
            placeholder=placeholder, options=options[:25], custom_id=custom_id
        )
    )
    return view


SETUP_CHOICES = [
    app_commands.Choice(name=v.nome_setup, value=v.id) for v in catalogo.vitrines()
] + [app_commands.Choice(name="Aviso Restock", value="AVISO_RESTOCK")]


class Admin(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="config", description="[ admin ]")
    @admin_only()
    async def config(self, interaction: discord.Interaction) -> None:
        view = static_view()
        view.add_item(
            discord.ui.Button(
                label="Canal de logs", custom_id="config_edit:logs_channel_id"
            )
        )
        view.add_item(
            discord.ui.Button(
                label="Logs ativos (1/0)", custom_id="config_edit:logs_ativo"
            )
        )
        embed = discord.Embed(
            title=f"{E.settings} Configurações do Bot", color=0x2B2D31
        )
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @app_commands.command(name="logs", description="[ admin ]")
    @admin_only()
    async def logs(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            f"{E.settings} Escolha uma categoria de logs:",
            view=_logs_categorias_view(),
            ephemeral=True,
        )

    @app_commands.command(name="estoque", description="[ admin ]")
    @admin_only()
    async def estoque(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            "Selecione o produto para gerenciar o estoque:",
            view=_product_select("estoque_select_produto", "Selecione um produto..."),
            ephemeral=True,
        )

    @app_commands.command(name="gerenciar", description="[ admin ]")
    @admin_only()
    async def gerenciar(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            embed=discord.Embed(
                title=f"{E.shield} Gerenciar Donos e Staff", color=0x2B2D31
            ),
            view=_team_view(dono=True),
            ephemeral=True,
        )

    @app_commands.command(name="staff", description="[ admin ]")
    @admin_only()
    async def staff(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            embed=discord.Embed(title=f"{E.shield} Gerenciar Equipe", color=0x2B2D31),
            view=_team_view(dono=False),
            ephemeral=True,
        )

    @app_commands.command(name="emoji", description="[ admin ]")
    @staff_only()
    async def emoji(self, interaction: discord.Interaction) -> None:
        options = []
        if interaction.guild:
            options = [
                discord.SelectOption(label=e.name, value=str(e.id), emoji=e)
                for e in interaction.guild.emojis[:25]
            ]
        view = static_view()
        view.add_item(
            discord.ui.Select(
                placeholder="Selecione um emoji para copiar ID...",
                options=options or [discord.SelectOption(label="Nenhum", value="0")],
                custom_id="emoji_select_copy",
            )
        )
        await interaction.response.send_message(
            "Selecione o emoji para copiar o ID:", view=view, ephemeral=True
        )

    @app_commands.command(name="setup", description="[ admin ]")
    @staff_only()
    @app_commands.choices(tipo=SETUP_CHOICES)
    async def setup(self, interaction: discord.Interaction, tipo: str) -> None:
        await interaction.response.defer(ephemeral=True)
        canal = interaction.channel
        store.set_config(f"{tipo.lower()}_channel_id", str(canal.id))
        try:
            if tipo != "AVISO_RESTOCK":
                await shelves.publish(tipo, fallback_channel=canal)
            await interaction.followup.send(
                f"{E.correct} Setup de `{tipo}` realizado com sucesso em {canal.mention}!\n"
                f"-# Canal salvo como `{tipo.lower()}_channel_id`.",
                ephemeral=True,
            )
        except Exception as exc:
            await interaction.followup.send(
                f"{E.wrong} Erro ao realizar setup: {exc}", ephemeral=True
            )

    @route(exact="logs_categoria")
    async def _logs_categoria(self, interaction: discord.Interaction, cid: str) -> None:
        if not store.is_admin(interaction.user.id):
            await interaction.response.send_message(
                f"{E.wrong} Sem permissão.", ephemeral=True
            )
            return
        categoria = interaction.data["values"][0]
        await interaction.response.edit_message(
            content=_logs_titulo(categoria), view=_logs_eventos_view(categoria)
        )

    @route(prefix="logs_toggle:")
    async def _logs_toggle(self, interaction: discord.Interaction, cid: str) -> None:
        if not store.is_admin(interaction.user.id):
            await interaction.response.send_message(
                f"{E.wrong} Sem permissão.", ephemeral=True
            )
            return
        evento = eventos.EVENTOS[cid.split(":", 1)[1]]
        eventos.definir(evento.id, not eventos.ativo(evento.id))
        await interaction.response.edit_message(
            content=_logs_titulo(evento.categoria),
            view=_logs_eventos_view(evento.categoria),
        )

    @route(prefix="logs_padrao:")
    async def _logs_padrao(self, interaction: discord.Interaction, cid: str) -> None:
        if not store.is_admin(interaction.user.id):
            await interaction.response.send_message(
                f"{E.wrong} Sem permissão.", ephemeral=True
            )
            return
        categoria = cid.split(":", 1)[1]
        eventos.restaurar_padrao(categoria)
        await interaction.response.edit_message(
            content=_logs_titulo(categoria), view=_logs_eventos_view(categoria)
        )

    @route(exact="logs_voltar")
    async def _logs_voltar(self, interaction: discord.Interaction, cid: str) -> None:
        await interaction.response.edit_message(
            content=f"{E.settings} Escolha uma categoria de logs:",
            view=_logs_categorias_view(),
        )

    @route(prefix="config_edit:")
    async def _config_edit(self, interaction: discord.Interaction, cid: str) -> None:
        await interaction.response.send_modal(ConfigModal(cid.split(":", 1)[1]))

    @route(exact="gerenciar_add_dono")
    async def _add_dono(self, interaction, cid):
        await interaction.response.send_modal(TeamModal("admin", True))

    @route(exact="gerenciar_rem_dono")
    async def _rem_dono(self, interaction, cid):
        await interaction.response.send_modal(TeamModal("admin", False))

    @route(exact="gerenciar_add_staff")
    async def _add_staff(self, interaction, cid):
        await interaction.response.send_modal(TeamModal("staff", True))

    @route(exact="gerenciar_rem_staff")
    async def _rem_staff(self, interaction, cid):
        await interaction.response.send_modal(TeamModal("staff", False))

    @route(exact="estoque_select_produto")
    async def _estoque_menu(self, interaction: discord.Interaction, cid: str) -> None:
        pk = interaction.data["values"][0]
        view = static_view()
        view.add_item(
            discord.ui.Button(
                label="Adicionar",
                style=discord.ButtonStyle.success,
                custom_id=f"estoque_add:{pk}",
            )
        )
        view.add_item(
            discord.ui.Button(
                label="Remover",
                style=discord.ButtonStyle.danger,
                custom_id=f"estoque_rem:{pk}",
            )
        )
        view.add_item(
            discord.ui.Button(
                label="Limpar Tudo",
                style=discord.ButtonStyle.danger,
                custom_id=f"estoque_limpar:{pk}",
            )
        )
        embed = discord.Embed(
            title=f"{E.cardbox} Estoque: {store.produto_nome(pk)}", color=0x2B2D31
        )
        await interaction.response.edit_message(content="", embed=embed, view=view)

    @route(prefix="estoque_add:")
    async def _estoque_add(self, interaction, cid):
        await interaction.response.send_modal(AddStockModal(cid.split(":", 1)[1]))

    @route(prefix="estoque_rem:")
    async def _estoque_rem(self, interaction, cid):
        await interaction.response.send_modal(RemoveStockModal(cid.split(":", 1)[1]))

    @route(prefix="estoque_limpar:")
    async def _estoque_limpar(self, interaction, cid):
        pk = cid.split(":", 1)[1]
        view = static_view()
        view.add_item(
            discord.ui.Button(
                label="Confirmar Limpeza",
                style=discord.ButtonStyle.danger,
                custom_id=f"estoque_limpar_confirm:{pk}",
            )
        )
        await interaction.response.send_message(
            "Tem certeza que deseja limpar todo o estoque deste produto?",
            view=view,
            ephemeral=True,
        )

    @route(prefix="estoque_limpar_confirm:")
    async def _estoque_limpar_confirm(self, interaction, cid):
        pk = cid.split(":", 1)[1]
        stock.clear(pk)
        await interaction.response.edit_message(
            content=f"{E.correct} Todo o estoque de `{store.produto_nome(pk)}` foi limpo!",
            view=None,
        )
        await shelves.refresh_for_product(pk)

    @route(exact="emoji_select_copy")
    async def _emoji_copy(self, interaction, cid):
        await interaction.response.send_message(
            f"ID do emoji: `{interaction.data['values'][0]}`", ephemeral=True
        )


def _logs_categorias_view() -> discord.ui.View:
    view = static_view()
    opcoes = [
        discord.SelectOption(label=nome, value=cid)
        for cid, nome in eventos.CATEGORIAS.items()
    ]
    view.add_item(
        discord.ui.Select(
            placeholder="Escolha uma categoria...",
            options=opcoes,
            custom_id="logs_categoria",
        )
    )
    return view


def _logs_eventos_view(categoria: str) -> discord.ui.View:
    view = static_view()
    for evento in eventos.da_categoria(categoria):
        estilo = (
            discord.ButtonStyle.success
            if eventos.ativo(evento.id)
            else discord.ButtonStyle.secondary
        )
        view.add_item(
            discord.ui.Button(
                label=evento.nome[:80],
                style=estilo,
                custom_id=f"logs_toggle:{evento.id}",
            )
        )
    view.add_item(
        discord.ui.Button(
            label="Restaurar padrão",
            style=discord.ButtonStyle.primary,
            custom_id=f"logs_padrao:{categoria}",
        )
    )
    view.add_item(
        discord.ui.Button(
            label="Voltar", style=discord.ButtonStyle.secondary, custom_id="logs_voltar"
        )
    )
    return view


def _logs_titulo(categoria: str) -> str:
    return f"{E.settings} **Logs: {eventos.CATEGORIAS[categoria]}**\n-# Verde = ligado · cinza = desligado. Clique para alternar."


def _team_view(*, dono: bool) -> discord.ui.View:
    view = static_view()
    if dono:
        view.add_item(
            discord.ui.Button(
                label="+ Dono",
                style=discord.ButtonStyle.success,
                custom_id="gerenciar_add_dono",
            )
        )
        view.add_item(
            discord.ui.Button(
                label="- Dono",
                style=discord.ButtonStyle.danger,
                custom_id="gerenciar_rem_dono",
            )
        )
    view.add_item(
        discord.ui.Button(
            label="+ Staff",
            style=discord.ButtonStyle.success,
            custom_id="gerenciar_add_staff",
        )
    )
    view.add_item(
        discord.ui.Button(
            label="- Staff",
            style=discord.ButtonStyle.danger,
            custom_id="gerenciar_rem_staff",
        )
    )
    return view


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Admin(bot))
