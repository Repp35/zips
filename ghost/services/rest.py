"""Chamadas REST diretas à API do Discord, usadas para mensagens com Components V2.

Rate limit: cada rota pertence a um bucket (X-RateLimit-Bucket). O cliente espera o reset
quando o bucket está sem requisições restantes, serializa as chamadas de um mesmo bucket e
respeita o limite global. Um 429 nunca descarta a requisição: espera o retry_after e repete.
"""

import asyncio
import logging
import math
import re
import time
from collections import deque
from urllib.parse import quote

import aiohttp

from ..config import settings

log = logging.getLogger("rest")

API = "https://discord.com/api/v10"
_session: aiohttp.ClientSession | None = None

LIMITE_GLOBAL_POR_S = 20  # limite conservador; divide o token com discord.py

_buckets: dict[str, "_Bucket"] = {}
_bucket_da_rota: dict[str, str] = {}
_janela_global: deque[float] = deque()
_pausa_global_ate = 0.0
_trava_posicoes_canais = asyncio.Lock()
_ultima_posicao_canais: float | None = None
INTERVALO_POSICOES_CANAIS_S = 5.1

_MENSAGEM = re.compile(r"(/messages/)\d+")
_TOKEN_WEBHOOK = re.compile(r"(/webhooks/\d+)/[^/?]+")


class _Bucket:
    __slots__ = ("trava", "restantes", "reset_em")

    def __init__(self) -> None:
        self.trava = asyncio.Lock()
        self.restantes: int | None = None
        self.reset_em = 0.0  # instante monotônico em que o bucket volta


async def _http() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
    return _session


class DiscordTransportError(RuntimeError):
    """A resposta não chegou; uma escrita pode ter sido aceita pelo Discord."""


class DiscordHTTPError(RuntimeError):
    def __init__(self, status: int, codigo: int | None = None, mensagem: str | None = None):
        detalhe = f"Discord HTTP {status}"
        if codigo is not None:
            detalhe += f" (código {codigo}: {mensagem or 'sem descrição'})"
        super().__init__(detalhe)
        self.status = status
        self.codigo = codigo
        self.mensagem = mensagem


async def close() -> None:
    if _session is not None and not _session.closed:
        await _session.close()


def _chave(method: str, path: str) -> str:
    """Rota sem IDs de mensagem nem token de webhook; o bucket real vem do cabeçalho."""
    limpo = _TOKEN_WEBHOOK.sub(r"\1/{token}", path.split("?", 1)[0])
    # Exclusões concorrentes usam o mesmo limitador conservador desde a
    # primeira chamada, mesmo que cada canal tenha um ID diferente.
    if method.upper() == "DELETE" and re.fullmatch(r"/channels/\d+", limpo):
        limpo = "/channels/{channel_id}"
    return f"{method} {_MENSAGEM.sub(r'\1:id', limpo)}"


def _bucket(chave: str) -> _Bucket:
    bid = _bucket_da_rota.get(chave, chave)
    return _buckets.setdefault(bid, _Bucket())


def _aprender(chave: str, bucket: _Bucket, headers) -> _Bucket:
    """Guarda o bucket real da rota e quantas requisições restam nele."""
    bid = headers.get("X-RateLimit-Bucket")
    if bid:
        _bucket_da_rota[chave] = bid
        bucket = _buckets.setdefault(bid, bucket)
    restantes = headers.get("X-RateLimit-Remaining")
    if restantes is not None:
        try:
            bucket.restantes = int(float(restantes))
        except ValueError:
            pass
    reset = headers.get("X-RateLimit-Reset-After")
    if reset is not None:
        try:
            bucket.reset_em = time.monotonic() + float(reset)
        except ValueError:
            pass
    return bucket


async def _aguardar_bucket(bucket: _Bucket) -> None:
    if bucket.restantes == 0:
        espera = bucket.reset_em - time.monotonic()
        if espera > 0:
            await asyncio.sleep(espera)
        bucket.restantes = None  # o próximo cabeçalho informa o estado novo


async def _aguardar_global() -> None:
    global _pausa_global_ate
    while True:
        agora = time.monotonic()
        if agora < _pausa_global_ate:
            await asyncio.sleep(_pausa_global_ate - agora)
            continue
        while _janela_global and agora - _janela_global[0] > 1.0:
            _janela_global.popleft()
        if len(_janela_global) < LIMITE_GLOBAL_POR_S:
            _janela_global.append(agora)
            return
        await asyncio.sleep(1.0 - (agora - _janela_global[0]))


def _ler(raw: str) -> dict | list:
    import json as json_module

    try:
        data = json_module.loads(raw) if raw else {}
    except ValueError:
        return {}
    return data if isinstance(data, (dict, list)) else {}


def _espera_429(data: dict | list, headers) -> float | None:
    bruto = data.get("retry_after") if isinstance(data, dict) else None
    if bruto is None:
        bruto = headers.get("Retry-After")
    try:
        espera = float(bruto)
    except (TypeError, ValueError):
        return None
    # retry_after é autoridade do servidor; não transformar uma espera longa
    # em erro imediato, porque o chamador pode então repetir em loop.
    if not math.isfinite(espera) or espera < 0:
        return None
    return espera


def _formulario(payload: dict, arquivos: list[tuple[str, bytes, str]]):
    """multipart/form-data: payload_json + files[i]. Recriado a cada tentativa (o corpo é consumido)."""
    import json as json_module

    form = aiohttp.FormData()
    form.add_field("payload_json", json_module.dumps(payload), content_type="application/json")
    for i, (nome, dados, tipo) in enumerate(arquivos):
        form.add_field(f"files[{i}]", dados, filename=nome, content_type=tipo)
    return form


async def request(
    method: str,
    path: str,
    json: dict | list | None = None,
    arquivos: list[tuple[str, bytes, str]] | None = None,
    *,
    reason: str | None = None,
) -> dict | list:
    global _pausa_global_ate
    headers = {"Authorization": f"Bot {settings.BOT_TOKEN}"}
    if reason:
        headers["X-Audit-Log-Reason"] = quote(reason, safe="")
    if not arquivos:
        headers["Content-Type"] = "application/json"
    http = await _http()
    chave = _chave(method, path)
    safe_path = "/" + path.lstrip("/").split("/", 1)[0]
    try:
        while True:
            bucket = _bucket(chave)
            async with bucket.trava:  # um bucket por vez: sem rajadas paralelas que geram 429
                await _aguardar_bucket(bucket)
                await _aguardar_global()
                corpo = {"json": json} if not arquivos else {"data": _formulario(json or {}, arquivos)}
                async with http.request(method, f"{API}{path}", headers=headers, **corpo) as resp:
                    raw = await resp.text()
                    data = _ler(raw)
                    # Use o bucket canônico também para registrar um 429.
                    # Antes, se a rota acabasse de ser associada a um bucket
                    # já conhecido, retry_after era gravado no objeto provisório
                    # e a repetição consultava outro objeto sem a pausa.
                    bucket = _aprender(chave, bucket, resp.headers)
                    if resp.status == 429:
                        espera = _espera_429(data, resp.headers)
                        if espera is None:
                            raise DiscordHTTPError(429)
                        escopo = resp.headers.get("X-RateLimit-Scope", "")
                        global_ = (
                            bool(data.get("global")) if isinstance(data, dict) else False
                        ) or resp.headers.get("X-RateLimit-Global", "").lower() == "true"
                        if global_ or escopo == "global":
                            _pausa_global_ate = time.monotonic() + espera
                        else:
                            bucket.restantes = 0
                            bucket.reset_em = time.monotonic() + espera
                        log.warning(
                            "rate limit em %s %s (%s); repetindo em %.1fs sem descartar a requisição",
                            method,
                            safe_path,
                            escopo or "sem escopo",
                            espera,
                        )
                        continue
                    if resp.status >= 400:
                        codigo = data.get("code") if isinstance(data, dict) else None
                        mensagem = data.get("message") if isinstance(data, dict) else None
                        log.error(
                            "%s %s → HTTP %s (código %s: %s)",
                            method,
                            safe_path,
                            resp.status,
                            codigo,
                            mensagem,
                        )
                        raise DiscordHTTPError(resp.status, codigo, mensagem)
                    return data
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        raise DiscordTransportError("Falha de rede na API do Discord") from exc


async def atualizar_posicoes_canais(guild_id: int, posicoes: list[dict]) -> None:
    """Aplica as posições de vários canais em uma chamada da rota de lote do Discord."""
    global _ultima_posicao_canais
    if not posicoes:
        return
    async with _trava_posicoes_canais:
        agora = time.monotonic()
        espera = (
            INTERVALO_POSICOES_CANAIS_S
            if _ultima_posicao_canais is None
            else INTERVALO_POSICOES_CANAIS_S - (agora - _ultima_posicao_canais)
        )
        if espera > 0:
            await asyncio.sleep(espera)
        await request(
            "PATCH",
            f"/guilds/{guild_id}/channels",
            posicoes,
            reason="Restaurar posições dos canais do backup",
        )
        _ultima_posicao_canais = time.monotonic()


async def send_message(channel_id: int, payload: dict) -> dict:
    payload = {
        "allowed_mentions": {"parse": ["users"], "replied_user": False},
        **payload,
    }
    return await request("POST", f"/channels/{channel_id}/messages", payload)


async def edit_message(channel_id: int, message_id: int, payload: dict) -> dict:
    # Mensagem antiga publicada com embed: o Discord só aceita trocar para Components V2 se embeds vier vazio.
    if payload.get("flags", 0) & (1 << 15):
        payload = {**payload, "embeds": []}
    return await request(
        "PATCH", f"/channels/{channel_id}/messages/{message_id}", payload
    )


async def send_dm(user, payload: dict) -> dict:
    canal = await user.create_dm()
    return await send_message(canal.id, payload)


async def responder(interaction, payload: dict, *, editar: bool = False) -> None:
    """Resposta à interação com payload de Components V2 (type 4 = nova mensagem, 7 = editar a atual)."""
    await request(
        "POST",
        f"/interactions/{interaction.id}/{interaction.token}/callback",
        {"type": 7 if editar else 4, "data": payload},
    )


LIMITE_EMOJI_BYTES = 256 * 1024
TIPOS_IMAGEM = ("image/png", "image/jpeg", "image/gif", "image/webp")


async def baixar_imagem(url: str) -> tuple[bytes, str]:
    """Baixa uma imagem para virar emoji. Recusa o que não for imagem ou for maior que o limite do Discord."""
    from .images import download

    try:
        return await download(url, LIMITE_EMOJI_BYTES, TIPOS_IMAGEM)
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError) as exc:
        raise RuntimeError("Não foi possível baixar a imagem pública") from exc


async def baixar_bytes(url: str, limite: int) -> bytes | None:
    """Baixa um anexo inteiro para guardar no backup. Devolve None se passar do limite ou falhar."""
    try:
        http = await _http()
        async with http.get(url) as resp:
            if resp.status != 200:
                return None
            total = 0
            partes = []
            async for pedaco in resp.content.iter_chunked(64 * 1024):
                total += len(pedaco)
                if total > limite:
                    return None
                partes.append(pedaco)
            return b"".join(partes)
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
        return None
