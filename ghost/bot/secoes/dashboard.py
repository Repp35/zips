"""[ dono ] Dashboard: vendas (com reembolsos descontados) e números do servidor. Fica no /gerenciar e também no /dashboard."""

import asyncio

import discord

from ...ui import layout
from ...catalogo import render
from ...data import orders
from ...ui.emojis import E
from ...services import rest
from . import painel, so_dono, voltar


async def convites_ativos(guild: discord.Guild | None) -> str:
    if guild is None:
        return "—"
    try:
        convites = await asyncio.wait_for(guild.invites(), timeout=2.0)
    except (asyncio.TimeoutError, discord.HTTPException):
        return "indisponível"
    agora = discord.utils.utcnow()
    return str(sum(1 for c in convites if c.expires_at is None or c.expires_at > agora))


async def conteudo(guild: discord.Guild | None) -> tuple[str, str]:
    """(bloco de vendas, bloco do servidor) em texto, prontos para um Text Display."""
    fin = orders.resumo_financeiro()
    membros = (
        str(guild.member_count)
        if guild is not None and guild.member_count is not None
        else "—"
    )
    vendas = (
        f"**Total ganho:** {render.brl(fin['ganho'])}\n"
        f"**Compras aprovadas:** {fin['compras']} (somam {render.brl(fin['bruto'])})\n"
        f"**Reembolsos:** {fin['reembolsos']} (somam {render.brl(fin['reembolsado'])})\n"
        f"**Clientes que já compraram:** {fin['clientes']}"
    )
    servidor = f"**Membros do servidor:** {membros}\n**Convites ativos:** {await convites_ativos(guild)}"
    return vendas, servidor


async def menu(guild: discord.Guild | None) -> dict:
    vendas, servidor = await conteudo(guild)
    return painel(
        f"{E.basket} Dashboard",
        layout.texto(vendas),
        layout.separador(),
        layout.texto(servidor),
        layout.separador(),
        layout.linha(render.botao("Atualizar", "ger:dashboard:menu", 2, "@reload")),
        voltar(),
    )


async def tratar(interaction: discord.Interaction, acao: str, alvo: list[str]) -> None:
    if not await so_dono(interaction):
        return
    await rest.responder(interaction, await menu(interaction.guild), editar=True)
