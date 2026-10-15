"""Emojis visuais do bot: assets em ghost/assets/emojis viram emojis do servidor no boot.

Uso no código: f"{E.correct} Pronto!" — o valor é resolvido na hora do envio,
então nunca use E dentro de constantes de módulo ou defaults de função.
Enquanto o emoji não existe no servidor, usa o unicode de fallback.
"""

import logging
from pathlib import Path

import discord

log = logging.getLogger("emojis")

ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets" / "emojis"
PREFIXO = "gh_"

# nome -> (fallback unicode)
EMOJIS: dict[str, str] = {
    "wrong": "❌",
    "correct": "✅",
    "alert": "⚠️",
    "bell": "🔔",
    "delete": "🗑️",
    "cardbox": "📦",
    "cart": "🛒",
    "clock": "⏰",
    "ticket": "🎟️",
    "shield": "🛡️",
    "member": "👤",
    "members": "👥",
    "information": "ℹ️",
    "reload": "🔄",
    "save": "💾",
    "settings": "⚙️",
    "basket": "🧺",
    "pix": "💠",
    "coupon": "🏷️",
    "truck": "🚚",
    "headset": "🎧",
    "receipt": "🧾",
    "plus": "➕",
    "minus": "➖",
    "link": "🔗",
    "calendar": "📅",
}

_ids: dict[str, int] = {}


class _Emoji:
    __slots__ = ("nome",)

    def __init__(self, nome: str) -> None:
        self.nome = nome

    def __str__(self) -> str:
        eid = _ids.get(self.nome)
        return f"<:{PREFIXO}{self.nome}:{eid}>" if eid else EMOJIS[self.nome]

    def payload(self) -> dict:
        """Formato cru para componentes enviados direto à API."""
        eid = _ids.get(self.nome)
        if eid:
            return {"name": f"{PREFIXO}{self.nome}", "id": str(eid)}
        return {"name": EMOJIS[self.nome]}

    def __repr__(self) -> str:
        return f"<emoji {self.nome}>"


class _Catalogo:
    def __getattr__(self, nome: str) -> _Emoji:
        if nome in EMOJIS:
            return _Emoji(nome)
        raise AttributeError(nome)


E = _Catalogo()


async def sync(guild: discord.Guild) -> None:
    """Cria no servidor os emojis que ainda não existem e guarda os IDs."""
    existentes = {e.name: e for e in guild.emojis}
    for nome in EMOJIS:
        chave = f"{PREFIXO}{nome}"
        if chave in existentes:
            _ids[nome] = existentes[chave].id
            continue
        try:
            imagem = (ASSETS_DIR / f"{nome}.png").read_bytes()
            emoji = await guild.create_custom_emoji(
                name=chave, image=imagem, reason="Assets visuais do bot"
            )
            _ids[nome] = emoji.id
            log.info("emoji criado: %s", chave)
        except (discord.HTTPException, OSError) as exc:
            log.error("não foi possível criar o emoji %s: %s", chave, exc)
    log.info("emojis prontos: %d/%d", len(_ids), len(EMOJIS))
