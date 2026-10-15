"""Log colorido no console e spinner do boot."""

import logging
import sys
import threading
import traceback

from datetime import datetime

RESET = "\033[0m"
DIM = "\033[38;5;244m"
OK = "\033[38;5;114m"
WARN = "\033[38;5;221m"
ERR = "\033[38;5;203m"
INFO = "\033[38;5;141m"

_lock = threading.Lock()


def _write(glyph: str, color: str, msg: str) -> None:
    stamp = datetime.now().strftime("%H:%M:%S")
    # Os logs são lidos tanto em terminais quanto em painéis que capturam
    # stdout/stderr. Desliga ANSI quando não há terminal interativo.
    stream = sys.stderr
    cor = color if getattr(stream, "isatty", lambda: False)() else ""
    reset = RESET if cor else ""
    linha = f"{DIM if cor else ''}{stamp}{reset}  {cor}{glyph}{reset} {msg}\n"
    with _lock:
        try:
            stream.write(linha)
            stream.flush()
        except UnicodeEncodeError:
            # Terminais Windows com codepage restrita ainda recebem o registro.
            dados = linha.encode(getattr(stream, "encoding", None) or "utf-8", "backslashreplace")
            buffer = getattr(stream, "buffer", None)
            if buffer is not None:
                buffer.write(dados)
                buffer.flush()
            else:
                stream.write(dados.decode("ascii"))
                stream.flush()


class ConsoleHandler(logging.Handler):
    _glyphs = {
        logging.DEBUG: ("·", DIM),
        logging.INFO: ("•", INFO),
        logging.WARNING: ("!", WARN),
        logging.ERROR: ("✗", ERR),
        logging.CRITICAL: ("✗", ERR),
    }

    def emit(self, record: logging.LogRecord) -> None:
        try:
            glyph, color = self._glyphs.get(record.levelno, ("•", INFO))
            origem = f"{record.filename}:{record.lineno}"
            _write(
                glyph,
                color,
                f"{record.levelname} [{record.name}] {origem} — {record.getMessage()}",
            )
            if record.exc_info:
                texto = "".join(traceback.format_exception(*record.exc_info)).rstrip()
                for linha in texto.splitlines():
                    _write(" ", DIM, linha)
            elif record.exc_text:
                for linha in record.exc_text.rstrip().splitlines():
                    _write(" ", DIM, linha)
        except Exception:
            self.handleError(record)


def setup_logging(level: int = logging.INFO) -> None:
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(ConsoleHandler())
    root.setLevel(level)
    logging.captureWarnings(True)
    for noisy in ("discord", "discord.http", "discord.gateway", "aiohttp"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def ok(msg: str) -> None:
    _write("✓", OK, msg)
