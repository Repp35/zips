"""Cliente REST do Mercado Pago para pagamentos PIX, com retry e chave de idempotência."""

import asyncio
import logging
import math
import uuid

import aiohttp

from ..config import settings

log = logging.getLogger("mercadopago")

API_BASE = "https://api.mercadopago.com"
RETRY_MAX = 3
RETRY_BASE_S = 0.8
RETRY_CAP_S = 6.0


class PixError(Exception):
    def __init__(
        self, message: str, *, status: int | None = None, payload: dict | None = None
    ):
        super().__init__(message)
        self.status = status
        self.payload = payload or {}


_session: aiohttp.ClientSession | None = None


async def session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=settings.MP_TIMEOUT_S)
        )
    return _session


async def close() -> None:
    global _session
    if _session is not None and not _session.closed:
        await _session.close()
    _session = None


def _error_message(payload: dict) -> str:
    if payload.get("message"):
        return f"{payload.get('error', '')}: {payload['message']}".strip(": ")
    causes = payload.get("cause")
    if isinstance(causes, list) and causes and isinstance(causes[0], dict):
        return _error_message(causes[0])
    return payload.get("error") or "erro desconhecido"


async def _request(
    method: str,
    path: str,
    *,
    body: dict | None = None,
    idempotency_key: str | None = None,
) -> dict:
    if not settings.MP_ACCESS_TOKEN:
        raise PixError("MP_ACCESS_TOKEN não configurado")
    headers = {
        "Authorization": f"Bearer {settings.MP_ACCESS_TOKEN}",
        "Accept": "application/json",
    }
    if idempotency_key:
        headers["X-Idempotency-Key"] = idempotency_key

    last: PixError | None = None
    for attempt in range(RETRY_MAX + 1):
        if attempt:
            await asyncio.sleep(min(RETRY_CAP_S, RETRY_BASE_S * 2 ** (attempt - 1)))
        try:
            http = await session()
            async with http.request(
                method, f"{API_BASE}{path}", json=body, headers=headers
            ) as resp:
                text = await resp.text()
                try:
                    payload = await resp.json(content_type=None) if text else {}
                except ValueError:
                    payload = {"raw": text[:300]}
                if not isinstance(payload, dict):
                    payload = {}
                if resp.status in (200, 201):
                    return payload if isinstance(payload, dict) else {}
                if resp.status == 429 or resp.status >= 500:
                    last = PixError(
                        f"HTTP {resp.status}", status=resp.status, payload=payload
                    )
                    log.warning(
                        "%s %s → %s (tentativa %d)",
                        method,
                        path,
                        resp.status,
                        attempt + 1,
                    )
                    continue
                raise PixError(
                    _error_message(payload), status=resp.status, payload=payload
                )
        except (asyncio.TimeoutError, aiohttp.ClientError) as exc:
            last = PixError(f"falha de rede: {type(exc).__name__}")
            log.warning(
                "%s %s → %s (tentativa %d)",
                method,
                path,
                type(exc).__name__,
                attempt + 1,
            )
    raise last or PixError("falha desconhecida")


async def create_pix(
    valor: float,
    descricao: str,
    external_reference: str,
    *,
    idempotency_key: str | None = None,
    payer_email: str | None = None,
) -> dict:
    if not math.isfinite(valor) or round(valor, 2) <= 0:
        raise PixError(f"valor inválido: {valor}")
    body = {
        "transaction_amount": round(valor, 2),
        "description": descricao[:200],
        "payment_method_id": "pix",
        "payer": {
            "email": payer_email or settings.PAYER_EMAIL,
            "first_name": "Cliente",
        },
        "external_reference": external_reference[:256],
        "statement_descriptor": "GHOST STORE",
    }
    payload = await _request(
        "POST",
        "/v1/payments",
        body=body,
        idempotency_key=idempotency_key or str(uuid.uuid4()),
    )
    if not payload.get("id"):
        raise PixError("Mercado Pago não retornou o id do pagamento", payload=payload)
    return payload


async def get_payment(payment_id: str | int) -> dict:
    return await _request("GET", f"/v1/payments/{payment_id}")


async def cancel_payment(payment_id: str | int) -> bool:
    try:
        payload = await _request(
            "PUT", f"/v1/payments/{payment_id}", body={"status": "cancelled"}
        )
        return payload.get("status") == "cancelled"
    except PixError as exc:
        if exc.status == 404:
            return True
        log.error("falha ao cancelar pagamento %s: %s", payment_id, exc)
        return False


async def refund(payment_id: str | int) -> dict:
    """Reembolso total de um pagamento aprovado (POST /v1/payments/{id}/refunds, sem corpo).
    A chave de idempotência é a mesma nas tentativas internas de _request, então um retry não devolve duas vezes."""
    return await _request(
        "POST",
        f"/v1/payments/{payment_id}/refunds",
        idempotency_key=f"ghost-refund-{payment_id}",
    )


def normalize_status(payment: dict) -> str:
    status = (payment.get("status") or "").lower()
    detail = (payment.get("status_detail") or "").lower()
    if status in ("refunded", "charged_back"):
        return "refunded"
    if status in ("approved", "cancelled", "rejected"):
        return status
    if detail in ("expired", "expired_by_timeout", "expired_by_payer"):
        return "expired"
    return "pending"
