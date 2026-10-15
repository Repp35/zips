"""Fechamento agendado de threads (só entregas concluídas), persistido no banco."""

import asyncio
import logging
from datetime import datetime, timedelta

import discord

from ..data import store
from . import context

log = logging.getLogger("scheduler")

_pending: dict[str, asyncio.Task] = {}


def schedule_close(thread_id: int, delay_s: float, motivo: str) -> None:
    fechar_em = (datetime.now() + timedelta(seconds=delay_s)).isoformat()
    schedule_id = store.schedule_close(thread_id, fechar_em, motivo)
    _spawn(thread_id, schedule_id, delay_s, motivo)


def cancel(thread_id: int | str) -> None:
    store.cancel_schedules(thread_id)
    task = _pending.pop(str(thread_id), None)
    if task:
        task.cancel()


def resume() -> None:
    agora = datetime.now()
    for row in store.pending_schedules():
        restante = (datetime.fromisoformat(row["fechar_em"]) - agora).total_seconds()
        _spawn(
            int(row["thread_id"]),
            row["id"],
            max(0.0, restante),
            row["motivo"] or "retomado",
        )


def _spawn(thread_id: int | str, schedule_id: int, delay_s: float, motivo: str) -> None:
    key = str(thread_id)
    previous = _pending.pop(key, None)
    if previous:
        previous.cancel()
    _pending[key] = asyncio.create_task(_run(thread_id, schedule_id, delay_s, motivo))


async def _run(
    thread_id: int | str, schedule_id: int, delay_s: float, motivo: str
) -> None:
    try:
        await asyncio.sleep(delay_s)
        try:
            await (await context.channel(int(thread_id))).delete()
            log.info("thread %s fechada (%s)", thread_id, motivo)
        except discord.NotFound:
            pass
        except discord.HTTPException as exc:
            log.warning("falha ao fechar thread %s: %s", thread_id, exc)
            return
        store.delete_schedule(schedule_id)
        store.forget_thread(thread_id)
    finally:
        if _pending.get(str(thread_id)) is asyncio.current_task():
            _pending.pop(str(thread_id), None)


async def close():
    tasks = tuple(_pending.values())
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    _pending.clear()
