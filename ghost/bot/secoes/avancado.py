"""[ dono ] Avançado: canais que o banco pode sobrepor e token do Mercado Pago.

- Canais: as chaves de settings.EDITAVEIS que são canal (exceto os de logs, que ficam em Logs). Trocar por select
  de canal grava no banco; "voltar ao padrão" apaga a sobra e volta ao config.json ou ao .env.
- IDs do servidor e do dono vêm do .env. Só aparecem: trocar em execução não refaz o registro dos comandos.
- Token do Mercado Pago: nunca vai para o banco nem aparece inteiro. Trocar vale só até o bot reiniciar;
  para ficar, o valor vai para MP_ACCESS_TOKEN no .env.
"""

import logging
import re

import discord

from ...ui import layout
from ...config import settings
from ...catalogo import render
from ...data import store
from ...ui.emojis import E
from ...services import envs, notify, rest
from ...config.settings import CORES
from . import aviso, painel, seletor_entidade, seletor_opcoes, so_dono, sem_acao, voltar

log = logging.getLogger("secao.avancado")

LOGS = {"logs_ativo", "logs_channel_id"}
CANAIS = [k for k in settings.EDITAVEIS if k.endswith("_channel_id") and k not in LOGS]
ROTULOS = {
    "fa_channel_id": "Canal FA",
    "nfa_channel_id": "Canal NFA",
    "suporte_channel_id": "Canal Suporte",
    "nitro_channel_id": "Canal Nitro",
    "capcut_channel_id": "Canal CapCut",
    "spotify_channel_id": "Canal Spotify",
    "pedido_restock_channel_id": "Canal de pedido de restock",
    "aviso_restock_channel_id": "Canal de aviso de restock",
}


def mascarar(token: str | None) -> str:
    """Só o tipo do token e os últimos 4 caracteres. Nunca o valor inteiro."""
    if not token:
        return "não definido"
    tipo = (
        "APP_USR"
        if token.startswith("APP_USR-")
        else "TEST"
        if token.startswith("TEST-")
        else "token"
    )
    return f"`{tipo}-…{token[-4:] if len(token) > 8 else '****'}`"


def _rotulo(chave: str) -> str:
    return ROTULOS.get(chave, chave)


def _origem(origem: str) -> str:
    """Texto amigável: 'personalizado' quando a equipe trocou, 'padrão' quando é o que vem de fábrica."""
    return {"banco": "personalizado", "config.json": "padrão", ".env": "padrão"}.get(
        origem, "não definido"
    )


def menu(confirmacao: str | None = None) -> dict:
    itens = []
    if confirmacao:
        itens.append(aviso(confirmacao))
    itens.append(
        layout.texto(
            f"**ID do servidor:** `{settings.GUILD_ID or 'não definido'}`\n"
            f"**Dono:** <@{settings.ADMIN_ID}>\n"
            f"**Token Mercado Pago:** {mascarar(settings.MP_ACCESS_TOKEN)}\n"
            "-# O ID do servidor e o do dono só mudam depois de reiniciar o bot."
        )
    )
    opcoes = []
    for chave in CANAIS:
        valor, origem = store.config_atual(chave)
        opcoes.append(
            {
                "label": _rotulo(chave)[:100],
                "value": chave,
                "description": f"{valor if valor is not None else 'não definido'} · {_origem(origem)}"[
                    :100
                ],
            }
        )
    itens.append(
        seletor_opcoes("ger:avancado:chave", "Escolha um canal para trocar...", opcoes)
    )
    itens.append(
        layout.linha(
            render.botao("ID do servidor", "ger:avancado:id:GUILD_ID", 2, "@settings"),
            render.botao("ID do dono", "ger:avancado:id:ADMIN_ID", 2, "@shield"),
            render.botao("Token do Mercado Pago", "ger:avancado:token", 2, "@shield"),
        )
    )
    itens.append(layout.linha(render.botao("Voltar", "ger:menu", 2, "@reload")))
    return painel(f"{E.settings} Avançado", *itens)


def canal_tela(chave: str, confirmacao: str | None = None) -> dict:
    valor, origem = store.config_atual(chave)
    itens = []
    if confirmacao:
        itens.append(aviso(confirmacao))
    itens.append(
        layout.texto(
            f"**{_rotulo(chave)}:** {f'<#{valor}>' if valor and valor.isdigit() else 'não definido'}"
            f"\n**Situação:** {_origem(origem)}\n-# Trocar o canal vale na hora."
        )
    )
    itens.append(
        seletor_entidade(
            "canal",
            f"ger:avancado:canal:{chave}",
            "Escolha o canal...",
            minimo=1,
            maximo=1,
            padrao=[valor] if valor and valor.isdigit() else None,
        )
    )
    itens.append(
        layout.linha(
            render.botao(
                "Voltar ao padrão", f"ger:avancado:padrao:{chave}", 2, "@reload"
            )
        )
    )
    itens.append(voltar("ger:avancado:menu", "Avançado"))
    return painel(f"{E.settings} {_rotulo(chave)}", *itens)


def token_tela(confirmacao: str | None = None) -> dict:
    itens = []
    if confirmacao:
        itens.append(aviso(confirmacao))
    itens.append(
        layout.texto(
            f"**Atual:** {mascarar(settings.MP_ACCESS_TOKEN)}\n"
            "-# Ao trocar, o novo token passa a valer na hora. "
            "Por segurança, ele não aparece completo aqui."
        )
    )
    itens.append(
        layout.linha(
            render.botao("Trocar token", "ger:avancado:token_editar", 1, "@save")
        )
    )
    itens.append(voltar("ger:avancado:menu", "Avançado"))
    return painel(f"{E.shield} Token do Mercado Pago", *itens)


class TokenModal(discord.ui.Modal):
    def __init__(self):
        super().__init__(title="Trocar token do Mercado Pago")
        self.token = discord.ui.TextInput(
            label="Access token (APP_USR-... ou TEST-...)", max_length=200
        )
        self.add_item(self.token)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        from ..security import authorize

        if not await authorize(interaction, "owner"):
            return
        novo = self.token.value.strip()
        if not re.fullmatch(r"(APP_USR|TEST)-[A-Za-z0-9-]{20,200}", novo):
            await interaction.response.send_message(
                f"{E.wrong} Isso não parece um access token do Mercado Pago (começa com APP_USR- ou TEST-).",
                ephemeral=True,
            )
            return
        _gravar_env("MP_ACCESS_TOKEN", novo)
        settings.MP_ACCESS_TOKEN = novo
        await notify.registrar(
            "config_alterada",
            f"{E.settings} Token do Mercado Pago trocado",
            CORES["aviso"],
            [
                ("Valor", mascarar(novo)),
                ("Gravado em", ".env"),
                ("Por", f"{interaction.user} ({interaction.user.id})"),
            ],
        )
        await rest.responder(
            interaction, token_tela(confirmacao="Token salvo."), editar=True
        )


_ROTULO_ID = {"GUILD_ID": "ID do servidor", "ADMIN_ID": "ID do dono"}


def _gravar_env(chave: str, valor: str) -> None:
    envs.gravar(chave, valor)


class IdModal(discord.ui.Modal):
    def __init__(self, chave: str):
        super().__init__(title=f"Trocar {_ROTULO_ID.get(chave, 'ID')}"[:45])
        self.chave = chave if chave in _ROTULO_ID else ""
        self.valor = discord.ui.TextInput(label="Novo ID (só números)", max_length=25)
        self.add_item(self.valor)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        from ..security import authorize

        if not await authorize(interaction, "owner"):
            return
        novo = self.valor.value.strip()
        if not self.chave or not novo.isdigit():
            await interaction.response.send_message(
                f"{E.wrong} Informe um ID numérico.", ephemeral=True
            )
            return
        _gravar_env(self.chave, novo)
        vale = "reiniciar o bot" if self.chave == "GUILD_ID" else "já valendo"
        await notify.registrar(
            "config_alterada",
            f"{E.settings} ID trocado",
            CORES["aviso"],
            [
                ("Chave", self.chave),
                ("Novo valor", novo),
                ("Vale", vale),
                ("Por", f"{interaction.user} ({interaction.user.id})"),
            ],
        )
        extra = " Reinicie o bot para valer." if self.chave == "GUILD_ID" else ""
        await interaction.response.send_message(
            f"{E.correct} {_ROTULO_ID[self.chave]} salvo.{extra}", ephemeral=True
        )


async def tratar(interaction: discord.Interaction, acao: str, alvo: list[str]) -> None:
    if not await so_dono(interaction):
        return
    if acao in ("", "menu"):
        return await rest.responder(interaction, menu(), editar=True)
    if acao == "chave":
        valores = interaction.data.get("values") or []
        if not valores or valores[0] not in CANAIS:
            return await sem_acao(interaction)
        return await rest.responder(interaction, canal_tela(valores[0]), editar=True)
    if acao == "id":
        return await interaction.response.send_modal(IdModal(alvo[0] if alvo else ""))
    if acao == "token":
        return await rest.responder(interaction, token_tela(), editar=True)
    if acao == "token_editar":
        return await interaction.response.send_modal(TokenModal())

    chave = alvo[0] if alvo else ""
    if chave not in CANAIS:
        return await sem_acao(interaction)
    if acao == "canal":
        valores = interaction.data.get("values") or []
        if not valores or not valores[0].isdigit():
            return await sem_acao(interaction)
        store.set_config(chave, valores[0])
        await notify.registrar(
            "config_alterada",
            f"{E.settings} Configuração alterada",
            CORES["info"],
            [
                ("Chave", chave),
                ("Novo valor", valores[0]),
                ("Por", f"{interaction.user} ({interaction.user.id})"),
            ],
        )
        return await rest.responder(
            interaction, canal_tela(chave, confirmacao="Canal salvo."), editar=True
        )
    if acao == "padrao":
        store.reset_config(chave)
        await notify.registrar(
            "config_alterada",
            f"{E.settings} Configuração alterada",
            CORES["info"],
            [
                ("Chave", chave),
                ("Novo valor", "voltou ao padrão"),
                ("Por", f"{interaction.user} ({interaction.user.id})"),
            ],
        )
        return await rest.responder(
            interaction, canal_tela(chave, confirmacao="Voltou ao padrão."), editar=True
        )
    return await sem_acao(interaction)
