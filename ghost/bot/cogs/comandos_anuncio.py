"""/anunciar e >anunciar: painel de componentes para montar um anúncio (título, texto, rodapé, imagem, cor e canal)
e enviá-lo com o botão ENVIAR.

O rascunho fica em memória, por usuário. Quem clica precisa ser admin, mesmo quando o painel está num canal
público (versão de prefixo), então cada componente confere o nível de novo.
"""

import logging
import re

import discord
from discord import app_commands
from discord.ext import commands

from ...ui import layout
from ...catalogo import render
from ...data import store
from ...ui.emojis import E
from ...services import notify, rest
from ...config.settings import CORES
from ..checks import admin_only
from ..router import route
from . import comandos_acoes as acoes

log = logging.getLogger("anuncio")

RASCUNHOS: dict[int, dict] = {}
# campo: (rótulo, limite de caracteres, texto longo)
CAMPOS = {
    "titulo": ("Título", 100, False),
    "texto": ("Texto", 2000, True),
    "rodape": ("Rodapé", 200, False),
    "imagem": ("Link da imagem (https)", 500, False),
    "cor": ("Cor, ex.: #3498db", 7, False),
}
CANAIS_TEXTO = [0, 5]  # texto e anúncio


def _rascunho(user_id: int) -> dict:
    return RASCUNHOS.setdefault(user_id, {"efemero": True})


def _cor(texto: str) -> int:
    m = re.fullmatch(r"#?([0-9a-fA-F]{6})", texto.strip())
    if not m:
        raise ValueError("A cor precisa ser hexadecimal, por exemplo #3498db.")
    return int(m.group(1), 16)


def _texto_atual(d: dict, campo: str) -> str:
    valor = d.get(campo)
    if campo == "cor" and valor is not None:
        return f"#{valor:06x}"
    return valor or ""


def _curto(texto: str | None, limite: int = 80) -> str:
    if not texto:
        return "—"
    return texto if len(texto) <= limite else f"{texto[:limite]}…"


def painel(user_id: int, aviso: str | None = None) -> dict:
    d = _rascunho(user_id)
    cor = f"#{d['cor']:06x}" if d.get("cor") is not None else "padrão do bot"
    canal = f"<#{d['canal']}>" if d.get("canal") else "não escolhido"
    resumo = "\n".join(
        [
            f"**Título:** {_curto(d.get('titulo'))}",
            f"**Texto:** {_curto(d.get('texto'))}",
            f"**Rodapé:** {_curto(d.get('rodape'))}",
            f"**Imagem:** {_curto(d.get('imagem'))}",
            f"**Cor:** {cor}",
            f"**Canal de destino:** {canal}",
        ]
    )
    seletor_canal = {
        "type": 8,
        "custom_id": "anu:canal",
        "placeholder": "Canal de destino",
        "channel_types": CANAIS_TEXTO,
        "min_values": 1,
        "max_values": 1,
    }
    if d.get("canal"):
        seletor_canal["default_values"] = [{"id": str(d["canal"]), "type": "channel"}]
    partes = [
        layout.titulo(f"{E.bell} Anúncio"),
        layout.texto(resumo),
        layout.separador(),
        layout.linha(
            render.botao("Título", "anu:editar:titulo", 2, "@information"),
            render.botao("Texto", "anu:editar:texto", 2, "@save"),
            render.botao("Rodapé", "anu:editar:rodape", 2, "@link"),
            render.botao("Imagem", "anu:editar:imagem", 2, "@cardbox"),
            render.botao("Cor", "anu:editar:cor", 2, "@shield"),
        ),
        layout.linha(seletor_canal),
        layout.linha(render.botao("ENVIAR", "anu:enviar", 3, "@correct")),
    ]
    if aviso:
        partes.append(layout.rodape(aviso))
    return layout.mensagem(
        layout.cartao(*partes, cor=CORES["neutro"]), efemera=d["efemero"]
    )


def anuncio(d: dict) -> dict:
    """O que é postado no canal: só o que foi preenchido."""
    corpo = []
    if d.get("titulo"):
        corpo.append(layout.titulo(d["titulo"]))
    if d.get("texto"):
        corpo.append(layout.texto(d["texto"]))
    if d.get("imagem"):
        corpo.append(layout.imagem(d["imagem"]))
    if d.get("rodape"):
        corpo.extend([layout.separador(), layout.rodape(d["rodape"])])
    cor = d["cor"] if d.get("cor") is not None else CORES["info"]
    return layout.mensagem(layout.cartao(*corpo, cor=cor))


class CampoModal(discord.ui.Modal):
    def __init__(self, campo: str, atual: str):
        rotulo, limite, longo = CAMPOS[campo]
        super().__init__(title=f"Anúncio: {rotulo}"[:45])
        self.campo = campo
        self.entrada = discord.ui.TextInput(
            label=rotulo[:45],
            style=discord.TextStyle.paragraph if longo else discord.TextStyle.short,
            max_length=limite,
            required=campo in ("titulo", "texto"),
            default=atual or None,
        )
        self.add_item(self.entrada)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        from ..security import authorize

        if not await authorize(interaction, "admin"):
            return
        valor = self.entrada.value.strip()
        try:
            if self.campo == "imagem" and valor and not valor.startswith("https://"):
                raise ValueError("A imagem precisa ser um link começando com https://.")
            novo = (
                (_cor(valor) if valor else None)
                if self.campo == "cor"
                else (valor or None)
            )
        except ValueError as exc:
            await interaction.response.send_message(f"{E.wrong} {exc}", ephemeral=True)
            return
        _rascunho(interaction.user.id)[self.campo] = novo
        await rest.responder(interaction, painel(interaction.user.id), editar=True)


class Anuncio(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="anunciar", description="[ admin ]")
    @admin_only()
    async def slash_anunciar(self, interaction: discord.Interaction) -> None:
        _rascunho(interaction.user.id)["efemero"] = True
        await rest.responder(interaction, painel(interaction.user.id))

    @commands.command(name="anunciar")
    @acoes.exige("admin")
    async def pref_anunciar(self, ctx: commands.Context) -> None:
        """>anunciar — mesmo painel, visível no canal (só admins conseguem usar os botões)."""
        _rascunho(ctx.author.id)["efemero"] = False
        await rest.send_message(ctx.channel.id, painel(ctx.author.id))

    @route(prefix="anu:")
    async def _componente(self, interaction: discord.Interaction, cid: str) -> None:
        if not store.is_admin(interaction.user.id):
            await interaction.response.send_message(
                f"{E.wrong} Sem permissão.", ephemeral=True
            )
            return
        uid = interaction.user.id
        partes = cid.split(":")
        acao = partes[1] if len(partes) > 1 else ""
        if acao == "editar" and len(partes) > 2 and partes[2] in CAMPOS:
            campo = partes[2]
            await interaction.response.send_modal(
                CampoModal(campo, _texto_atual(_rascunho(uid), campo))
            )
            return
        if acao == "canal":
            _rascunho(uid)["canal"] = int(interaction.data["values"][0])
            await rest.responder(interaction, painel(uid), editar=True)
            return
        if acao == "enviar":
            await self._enviar(interaction)
            return
        await interaction.response.send_message(
            f"{E.wrong} Ação desconhecida.", ephemeral=True
        )

    async def _enviar(self, interaction: discord.Interaction) -> None:
        uid = interaction.user.id
        d = _rascunho(uid)
        erro = None
        if not d.get("canal"):
            erro = "Escolha o canal de destino."
        elif not (d.get("titulo") or d.get("texto")):
            erro = "Preencha o título ou o texto."
        if erro:
            await interaction.response.send_message(f"{E.wrong} {erro}", ephemeral=True)
            return
        canal = d["canal"]
        try:
            await rest.send_message(canal, anuncio(d))
        except RuntimeError as exc:
            await interaction.response.send_message(
                f"{E.wrong} Não consegui postar: {exc}", ephemeral=True
            )
            return
        await notify.registrar(
            "anuncio_enviado",
            f"{E.bell} Anúncio enviado",
            CORES["info"],
            [
                ("Canal", str(canal)),
                ("Título", d.get("titulo") or "—"),
                ("Por", f"{interaction.user} ({uid})"),
            ],
        )
        RASCUNHOS[uid] = {"efemero": d.get("efemero", True)}
        await rest.responder(
            interaction,
            painel(uid, aviso=f"{E.correct} Anúncio enviado em <#{canal}>."),
            editar=True,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Anuncio(bot))
