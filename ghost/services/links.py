"""Bloqueio de sites proibidos, por lista (ghost/config/links_proibidos.txt) e por imitação do Discord.

- Lista: bloqueia o domínio e os subdomínios (exemplo.com também bloqueia sub.exemplo.com).
- Imitação do Discord: domínio com a palavra "discord" ou "nitro" como parte separada do nome
  (ex.: discord-nitro-gift.xyz), fora dos domínios oficiais. "discordia.com.br" não é imitação.
- Exceções em [permitidos] nunca são bloqueadas.
"""

import logging
import re
from pathlib import Path

log = logging.getLogger("links")

NIVEIS = ("alto", "medio", "baixo")
_OFICIAIS = ("discord.com", "discord.gg", "discordapp.com", "discord.media", "discord.new", "discord.gift")
_TOKENS_DISCORD = {"discord", "nitro"}
# Domínio: letras, números e hífen, com ao menos um ponto. Com ou sem esquema (http/https/www).
_HOST = re.compile(
    r"(?:https?://)?(?:www\.)?((?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,})(?=[/:?#\s]|$)",
    re.IGNORECASE,
)


def carregar(caminho: Path) -> dict[str, set[str]]:
    niveis: dict[str, set[str]] = {n: set() for n in (*NIVEIS, "permitidos")}
    atual = None
    for linha in caminho.read_text(encoding="utf-8").splitlines():
        l = linha.strip().lower()
        if not l or l.startswith("#"):
            continue
        if l.startswith("[") and l.endswith("]"):
            nome = l[1:-1]
            atual = nome if nome in niveis else None
            continue
        if atual:
            niveis[atual].add(re.sub(r"^(https?://)?(www\.)?", "", l).split("/")[0])
    return niveis


def _dominio_em(host: str, conjunto: set[str]) -> bool:
    return any(host == d or host.endswith("." + d) for d in conjunto)


def hosts_em(texto: str) -> list[str]:
    return [m.group(1).lower() for m in _HOST.finditer(texto or "")]


def _imitacao_discord(host: str) -> bool:
    if _dominio_em(host, set(_OFICIAIS)):
        return False
    partes = set(re.split(r"[.\-]", host))
    return bool(partes & _TOKENS_DISCORD)


class Bloqueio:
    def __init__(self, listas: dict[str, set[str]]) -> None:
        self.listas = listas
        self.permitidos = listas.get("permitidos", set()) | set(_OFICIAIS)

    def achar(self, texto: str) -> tuple[str, str] | None:
        """Maior nível de bloqueio: (nível, domínio). None se o texto não tem site proibido."""
        melhor = None
        for host in hosts_em(texto):
            if _dominio_em(host, self.permitidos):
                continue
            nivel = None
            if _imitacao_discord(host):
                nivel = "medio"
            for n in NIVEIS:
                if _dominio_em(host, self.listas.get(n, set())):
                    nivel = n
                    break
            if nivel and (melhor is None or NIVEIS.index(nivel) < NIVEIS.index(melhor[0])):
                melhor = (nivel, host)
        return melhor


_cache: dict = {"caminho": None, "mtime": None, "bloqueio": None}


def obter(caminho: Path) -> Bloqueio:
    try:
        mtime = caminho.stat().st_mtime
    except OSError:
        if _cache["bloqueio"] is None:
            log.warning("arquivo de links proibidos não encontrado: %s", caminho)
            _cache.update(caminho=caminho, mtime=None, bloqueio=Bloqueio({}))
        return _cache["bloqueio"]
    if _cache["caminho"] != caminho or _cache["mtime"] != mtime:
        _cache.update(caminho=caminho, mtime=mtime, bloqueio=Bloqueio(carregar(caminho)))
    return _cache["bloqueio"]
