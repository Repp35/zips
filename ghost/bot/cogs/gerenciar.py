"""/gerenciar: painel de administração em seções. Cada botão abre uma tela, um formulário ou uma ação.

O hub só monta o menu e roteia `ger:<seção>:` para o módulo da seção (ghost/bot/secoes). Equipe fica aqui.
Cada módulo confere o próprio nível: [ dono ] ou [ admin ].
"""

import logging

import discord
from discord import app_commands
from discord.ext import commands

from ...ui import layout
from ...config import settings
from ...catalogo import render
from ...data import store
from ...ui.emojis import E
from ...services import equipe as cargo_equipe, notify, rest
from ...config.settings import CORES
from ..checks import admin_only
from ..router import route
from ..secoes import (
    antiraid,
    avancado,
    backup,
    cargos,
    cupom,
    estoque,
    logs,
    produto,
    dashboard,
)

log = logging.getLogger("gerenciar")

ROTULO_TIPO = {"admin": "Admin"}
SECOES = {
    "produto": produto,
    "estoque": estoque,
    "cupom": cupom,
    "cargos": cargos,
    "logs": logs,
    "avancado": avancado,
    "antiraid": antiraid,
    "backup": backup,
    "dashboard": dashboard,
}


def _e_dono(interaction: discord.Interaction) -> bool:
    return interaction.user.id == settings.ADMIN_ID


def _resolver(texto: str, guild: discord.Guild | None) -> int | None:
    """ID numérico ou username (com ou sem @) de um membro do servidor."""
    alvo = texto.strip().lstrip("@")
    if alvo.isdigit():
        return int(alvo)
    if guild is None:
        return None
    membro = guild.get_member_named(alvo)
    return membro.id if membro else None


def _equipe_texto() -> str:
    admins = store.listar_equipe("admin")
    return (
        f"**Dono:** <@{settings.ADMIN_ID}>\n"
        f"**Admins:** {' '.join(f'<@{i}>' for i in admins) or 'ninguém'}"
    )


async def _rotulo(guild: discord.Guild | None, uid: int) -> str:
    """Username do membro (@nome). Se não estiver no cache, busca no servidor; sem ele, mostra o ID."""
    membro = guild.get_member(uid) if guild else None
    if membro is None and guild is not None:
        try:
            membro = await guild.fetch_member(uid)
        except discord.HTTPException:
            membro = None
    return f"@{membro.name}" if membro else f"ID {uid}"


async def _equipe_opcoes(guild: discord.Guild | None) -> list[dict]:
    opcoes = []
    for tipo in ("admin",):
        for uid in store.listar_equipe(tipo):
            rotulo = await _rotulo(guild, int(uid))
            opcoes.append(
                {
                    "label": f"{ROTULO_TIPO[tipo]}: {rotulo}"[:100],
                    "value": f"{tipo}:{uid}",
                }
            )
    return opcoes[:25]


def menu_payload() -> dict:
    """Botões da tela principal. Todos aparecem para o admin; as ações de dono respondem "Só o dono" nas seções."""
    linha_1 = [
        render.botao("Equipe", "ger:eq:menu", 2, "@shield"),
        render.botao("Cupom", "ger:cupom:menu", 2, "@coupon"),
        render.botao("Estoque", "ger:estoque:menu", 2, "@cardbox"),
        render.botao("Produto", "ger:produto:menu", 2, "@cardbox"),
        render.botao("Cargos", "ger:cargos:menu", 2, "@shield"),
    ]
    linha_2 = [
        render.botao("Logs", "ger:logs:menu", 2, "@bell"),
        render.botao("Anti-raid", "ger:antiraid:menu", 2, "@shield"),
        render.botao("Backup", "ger:backup:menu", 2, "@save"),
        render.botao("Dashboard", "ger:dashboard:menu", 2, "@cart"),
        render.botao("Avançado", "ger:avancado:menu", 2, "@settings"),
    ]
    return layout.mensagem(
        layout.cartao(
            layout.titulo(f"{E.settings} Gerenciar"),
            layout.texto("Escolha uma seção."),
            layout.linha(*linha_1),
            layout.linha(*linha_2),
            cor=CORES["neutro"],
        ),
        efemera=True,
    )


async def equipe_payload(dono: bool, guild: discord.Guild | None) -> dict:
    partes = [
        layout.titulo(f"{E.shield} Equipe"),
        layout.texto(_equipe_texto()),
        layout.linha(
            *(
                [render.botao("+ Admin", "ger:eq:add:admin", 3, "@shield")]
                if dono
                else []
            ),
            render.botao("Voltar", "ger:menu", 2, "@reload"),
        ),
    ]
    opcoes = await _equipe_opcoes(guild)
    if opcoes:
        partes.append(
            layout.linha(layout.seletor("ger:eq:rem", "Remover admin...", opcoes))
        )
    else:
        partes.append(layout.texto("-# Ninguém para remover."))
    return layout.mensagem(layout.cartao(*partes, cor=CORES["neutro"]), efemera=True)


class EquipeModal(discord.ui.Modal):
    alvo = discord.ui.TextInput(
        label="ID ou @usuário", placeholder="1234567890 ou @nome", max_length=40
    )

    def __init__(self, tipo: str):
        super().__init__(title=f"Adicionar {ROTULO_TIPO[tipo]}")
        self.tipo = tipo

    async def on_submit(self, interaction: discord.Interaction) -> None:
        from ..security import authorize

        if not await authorize(interaction, "owner"):
            return
        if self.tipo == "admin" and not _e_dono(interaction):
            await interaction.response.send_message(
                f"{E.wrong} Só o dono adiciona admin.", ephemeral=True
            )
            return
        uid = _resolver(self.alvo.value, interaction.guild)
        if uid is None:
            await interaction.response.send_message(
                f"{E.wrong} Não encontrei esse usuário no servidor.", ephemeral=True
            )
            return
        if uid == settings.ADMIN_ID:
            await interaction.response.send_message(
                f"{E.wrong} Esse já é o dono.", ephemeral=True
            )
            return
        store.set_team_member(self.tipo, str(uid), interaction.user.id, True)
        await cargo_equipe.aplicar(interaction.guild, uid, True)
        await notify.registrar(
            "equipe_alterada",
            f"{E.shield} Equipe alterada",
            CORES["info"],
            [
                ("Ação", "adicionado"),
                ("Tipo", self.tipo),
                ("Usuário", str(uid)),
                ("Por", f"{interaction.user} ({interaction.user.id})"),
            ],
        )
        await interaction.response.send_message(
            f"{E.correct} <@{uid}> adicionado como {ROTULO_TIPO[self.tipo]}.",
            ephemeral=True,
        )


class Gerenciar(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="gerenciar", description="[ admin ]")
    @admin_only()
    async def gerenciar(self, interaction: discord.Interaction) -> None:
        await rest.responder(interaction, menu_payload())

    @route(prefix="ger:")
    async def _componente(self, interaction: discord.Interaction, cid: str) -> None:
        if not store.is_admin(interaction.user.id):
            await interaction.response.send_message(
                f"{E.wrong} Sem permissão.", ephemeral=True
            )
            return
        partes = cid.split(":")
        secao = partes[1] if len(partes) > 1 else ""
        acao = partes[2] if len(partes) > 2 else ""
        if secao == "menu" or cid == "ger:menu":
            await rest.responder(interaction, menu_payload(), editar=True)
            return
        if secao == "eq":
            await self._equipe(interaction, acao, partes[3:])
            return
        modulo = SECOES.get(secao)
        if modulo is None:
            await interaction.response.send_message(
                f"{E.wrong} Ação desconhecida.", ephemeral=True
            )
            return
        await modulo.tratar(interaction, acao, partes[3:])

    async def _equipe(
        self, interaction: discord.Interaction, acao: str, alvo: list[str]
    ) -> None:
        if acao == "menu":
            await rest.responder(
                interaction,
                await equipe_payload(_e_dono(interaction), interaction.guild),
                editar=True,
            )
            return
        if acao == "add":
            tipo = alvo[0] if alvo else ""
            if tipo == "admin" and not _e_dono(interaction):
                await interaction.response.send_message(
                    f"{E.wrong} Só o dono adiciona admin.", ephemeral=True
                )
                return
            if tipo not in ROTULO_TIPO:
                await interaction.response.send_message(
                    f"{E.wrong} Tipo inválido.", ephemeral=True
                )
                return
            await interaction.response.send_modal(EquipeModal(tipo))
            return
        if acao == "rem":
            tipo, _, uid = interaction.data["values"][0].partition(":")
            if tipo == "admin" and not _e_dono(interaction):
                await interaction.response.send_message(
                    f"{E.wrong} Só o dono remove admin.", ephemeral=True
                )
                return
            if tipo not in ROTULO_TIPO or not uid.isdigit():
                await interaction.response.send_message(
                    f"{E.wrong} Seleção inválida.", ephemeral=True
                )
                return
            store.set_team_member(tipo, uid, interaction.user.id, False)
            await cargo_equipe.aplicar(interaction.guild, int(uid), False)
            await notify.registrar(
                "equipe_alterada",
                f"{E.shield} Equipe alterada",
                CORES["info"],
                [
                    ("Ação", "removido"),
                    ("Tipo", tipo),
                    ("Usuário", uid),
                    ("Por", f"{interaction.user} ({interaction.user.id})"),
                ],
            )
            await rest.responder(
                interaction,
                await equipe_payload(_e_dono(interaction), interaction.guild),
                editar=True,
            )
            return
        await interaction.response.send_message(
            f"{E.wrong} Ação desconhecida.", ephemeral=True
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Gerenciar(bot))
