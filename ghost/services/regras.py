"""Regras puras de restauração e anti-spam, sem dependência do Discord (testáveis isoladamente)."""

import re
import time
from collections import defaultdict
from datetime import datetime
from urllib.parse import parse_qs, urlparse

from ..config import settings

SPAM_MENSAGENS = settings.SPAM_SEQUENCIA_MAX_MSGS
SPAM_INTERVALO_S = settings.SPAM_SEQUENCIA_INTERVALO_S
TIMEOUT_SPAM_S = 28 * 24 * 3600  # máximo que o Discord aceita
NOME_MAX = 80
LIMITE_ARQUIVO_PADRAO = 25 * 1024 * 1024  # usado só se o servidor não informar o limite
MAX_ARQUIVOS_POR_MENSAGEM = 10


class DetectorSequencia:
    """Conta mensagens seguidas do mesmo autor no mesmo canal.

    `registrar` devolve None enquanto não há spam. Ao atingir o limite, devolve (IDs, True),
    com todas as mensagens da sequência para apagar e aplicar uma única sanção. A continuação
    devolve ([id], False): apaga a nova mensagem sem escalar a pena várias vezes no mesmo ataque.
    """

    def __init__(self, limite: int = SPAM_MENSAGENS, intervalo_s: float = SPAM_INTERVALO_S):
        self.limite = limite
        self.intervalo_s = intervalo_s
        self._seq: dict[tuple, list[tuple[float, int]]] = defaultdict(list)
        self._disparada: set[tuple] = set()

    def registrar(self, canal, autor, msg_id: int, agora: float | None = None) -> tuple[list[int], bool] | None:
        agora = time.monotonic() if agora is None else agora
        chave = (canal, autor)
        seq = self._seq[chave]
        if seq and agora - seq[-1][0] > self.intervalo_s:
            seq.clear()
            self._disparada.discard(chave)
        seq.append((agora, msg_id))
        if chave in self._disparada:
            return [msg_id], False
        if len(seq) >= self.limite:
            self._disparada.add(chave)
            return [m for _, m in seq], True
        return None

    def encerrar(self, canal, autor) -> None:
        self._seq.pop((canal, autor), None)
        self._disparada.discard((canal, autor))


def expiracao_url(url: str | None) -> int | None:
    """Timestamp (segundos) em que um link de anexo do Discord expira, ou None se não tiver `ex`."""
    if not url:
        return None
    valor = parse_qs(urlparse(url).query).get("ex", [None])[0]
    if not valor:
        return None
    try:
        return int(valor, 16)
    except ValueError:
        return None


def url_valida(url: str | None, agora: float) -> bool:
    if not url:
        return False
    ex = expiracao_url(url)
    return ex is None or ex > agora


def e_midia(content_type: str | None, nome: str | None) -> bool:
    """Imagem, GIF e vídeo: vão como link, para o Discord mostrar o preview. Demais arquivos vão como arquivo."""
    tipo = (content_type or "").lower()
    if tipo.startswith(("image/", "video/")):
        return True
    return (nome or "").lower().endswith(
        (".png", ".jpg", ".jpeg", ".gif", ".webp", ".mp4", ".mov", ".webm")
    )


_LINK_ENTRE_COLCHETES = re.compile(r"<(https?://[^\s<>]+)>")


def limpar_links(texto: str) -> str:
    """Links puros, sem <>, para o Discord gerar o preview igual ao original."""
    return _LINK_ENTRE_COLCHETES.sub(r"\1", texto or "")


def nome_mensagem(apelido: str | None, hora: str | None) -> str:
    """Apelido primeiro, depois data e hora dd/mm/aaaa hh:mm:ss. Máximo de 80 caracteres."""
    base = (apelido or "Membro").strip() or "Membro"
    nome = f"{base} {hora}" if hora else base
    return nome[:NOME_MAX]


def hora_de_belem(iso: str | None) -> str:
    if not iso:
        return ""
    from datetime import timedelta, timezone

    try:
        return (
            datetime.fromisoformat(iso)
            .astimezone(timezone(timedelta(hours=-3)))
            .strftime("%d/%m/%Y %H:%M:%S")
        )
    except ValueError:
        return ""


def ordem_canais(canais: list[dict], atuais_por_id: dict[str, int]) -> list[dict]:
    """Ordem de criação/posição: a posição salva no backup; sem ela, a posição atual do servidor."""
    def chave(c):
        pos = c.get("posicao")
        if pos is None:
            pos = atuais_por_id.get(str(c.get("id")), 0)
        return (c.get("tipo") != "categoria", pos)

    return sorted(canais, key=chave)


# ── Escada de punição por nível ──────────────────────────────────────────────
# Cada nível tem passos por ocorrência dentro da janela. Depois do último passo, vem o ban.
MINUTO = 60
HORA = 3600
DIA = 86400
OCORRENCIA_JANELA_S = settings.PENALIDADE_JANELA_S

ESCALAS: dict[str, list[dict]] = {
    "baixo": [
        {"aviso": True, "cargos": False, "timeout_s": None},
        {"aviso": False, "cargos": True, "timeout_s": settings.SPAM_TIMEOUT_S},
    ],  # 3ª ocorrência: ban
    "medio": [
        {"aviso": False, "cargos": True, "timeout_s": settings.PENALIDADE_TIMEOUT_MEDIO_S},
    ],  # 2ª ocorrência: ban
    "grave": [
        {"aviso": False, "cargos": True, "timeout_s": settings.PENALIDADE_TIMEOUT_GRAVE_S, "renovar": True},
    ],  # 2ª ocorrência: ban; a primeira pena fica renovada para análise
}


def sancao(nivel: str, ocorrencia: int) -> dict:
    """Pena para a N-ésima ocorrência do nível dentro da janela. Passou da escada: ban."""
    passos = ESCALAS[nivel]
    if 1 <= ocorrencia <= len(passos):
        passo = passos[ocorrencia - 1]
        return {
            "ban": False,
            "aviso": passo.get("aviso", False),
            "cargos": passo["cargos"],
            "timeout_s": passo["timeout_s"],
            "renovar": passo.get("renovar", False),
        }
    return {"ban": True, "aviso": False, "cargos": True, "timeout_s": None, "renovar": False}


def ocorrencias_na_janela(registros: list[float], agora: float, janela: int = OCORRENCIA_JANELA_S) -> list[float]:
    return [t for t in registros if agora - t <= janela]
