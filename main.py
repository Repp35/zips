"""Ponto de entrada do Ghost Store Bot."""

import asyncio
import logging
import os

from ghost.config import settings
from ghost.bot.app import GhostBot
from ghost.core.logs import ok, setup_logging


async def run() -> None:
    if not settings.BOT_TOKEN:
        raise SystemExit("BOT_TOKEN não definido. Copie .env.example para .env e preencha.")
    faltando = [nome for nome in ("ADMIN_ID", "GUILD_ID") if not getattr(settings, nome)]
    if faltando:
        raise SystemExit(f"Defina no .env: {', '.join(faltando)}")
    bot = GhostBot()
    async with bot:
        await bot.start(settings.BOT_TOKEN)


def main() -> None:
    os.umask(0o077)
    setup_logging(logging.INFO)
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        ok("encerrado")


if __name__ == "__main__":
    main()
