"""Cargos do bot guardados por ID.

O nome do cargo na configuração (config.json) só serve para achar o cargo na primeira vez. Depois disso, o ID
salvo é a única referência: trocar o nome no Discord não quebra nada. Se o ID sumir (cargo apagado), o bot não
adivinha pelo nome; avisa no log e espera alguém escolher o cargo de novo.
"""

import logging

import discord

from ..config import settings
from ..data import store

log = logging.getLogger("papeis")

NOMES_PADRAO = {
    "membro": settings.CARGO_MEMBRO,
    "equipe": settings.CARGO_EQUIPE,
    "cliente": settings.CARGO_CLIENTE,
    "bots": settings.CARGO_BOTS,
}


def fixo(guild: discord.Guild | None, papel: str) -> discord.Role | None:
    """Cargo fixo do bot (membro, equipe, cliente). Só procura pelo nome quando ainda não há ID salvo."""
    if guild is None:
        return None
    chave = f"cargo_{papel}_id"
    salvo = store.get_config(chave)
    if salvo:
        cargo = guild.get_role(int(salvo))
        if cargo is None:
            log.warning(
                "cargo '%s' (ID %s) não existe mais no servidor; escolha outro no /gerenciar",
                papel,
                salvo,
            )
        return cargo
    achado = discord.utils.get(guild.roles, name=NOMES_PADRAO[papel])
    if achado is not None:
        store.set_config(chave, str(achado.id))
        log.info("cargo '%s' guardado pelo ID %s", papel, achado.id)
    else:
        log.warning(
            "cargo '%s' ('%s') não existe no servidor", papel, NOMES_PADRAO[papel]
        )
    return achado


async def dar_membro_a_todos(guild: discord.Guild) -> int:
    """Quem está no servidor e ainda não tem o cargo membro recebe. Bots não recebem. Devolve quantos receberam."""
    cargo = fixo(guild, "membro")
    if cargo is None:
        return 0
    feitos = 0
    for membro in guild.members:
        if membro.bot or cargo in membro.roles:
            continue
        try:
            await membro.add_roles(
                cargo, reason="Cargo membro para quem já está no servidor"
            )
            feitos += 1
        except discord.HTTPException as exc:
            log.warning("não consegui dar membro a %s: %s", membro.id, exc)
    return feitos


def do_nivel(guild: discord.Guild | None, nivel: dict) -> discord.Role | None:
    """Cargo de um nível de compra ou mensagem, sempre pelo ID salvo no nível."""
    if guild is None or not nivel.get("cargo_id"):
        return None
    return guild.get_role(int(nivel["cargo_id"]))


def id_do_nivel(nivel: dict) -> int | None:
    return int(nivel["cargo_id"]) if nivel.get("cargo_id") else None
