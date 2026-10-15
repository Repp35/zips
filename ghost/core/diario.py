"""Diário de segurança para eventos de negócio e snapshots do catálogo.

Formato JSON Lines (diario.jsonl): cada linha é um JSON completo, então um erro não corrompe o histórico
anterior. SQL bruto e valores de parâmetros não são registrados para evitar exposição de dados privados.
"""

import json
import logging
import os
import threading
from collections import deque
from datetime import datetime
from pathlib import Path

from ..config import settings

log = logging.getLogger("diario")
_diario_configurado = os.getenv("DIARIO_PATH", "").strip()
if _diario_configurado:
    _diario_path = Path(_diario_configurado).expanduser()
    ARQUIVO = _diario_path if _diario_path.is_absolute() else settings.BASE_DIR / _diario_path
else:
    ARQUIVO = settings.BASE_DIR / "data" / settings.DIARIO_ARQUIVO
_lock = threading.Lock()
_CATALOGO = Path(__file__).resolve().parent.parent / "catalogo"


def _gravar(linhas: list[dict]) -> None:
    if not linhas:
        return
    try:
        with _lock:
            ARQUIVO.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(ARQUIVO, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as arquivo:
                for linha in linhas:
                    arquivo.write(
                        json.dumps(linha, ensure_ascii=False, default=str) + "\n"
                    )
                arquivo.flush()
                os.fsync(arquivo.fileno())
    except OSError:
        log.exception("falha ao gravar no diário %s", ARQUIVO)


def _agora() -> str:
    return datetime.now().isoformat(timespec="milliseconds")


def evento(evento_id: str, titulo: str, campos) -> None:
    _gravar(
        [
            {
                "quando": _agora(),
                "tipo": "evento",
                "evento": evento_id,
                "titulo": titulo,
                "campos": [[str(n), str(v)] for n, v in campos],
            }
        ]
    )


def ultimos_eventos(limite: int = 1000) -> list[dict]:
    """Eventos mais recentes primeiro, até `limite`. Linhas que não lêem como JSON são ignoradas."""
    recentes: deque[dict] = deque(maxlen=limite)
    try:
        with open(ARQUIVO, encoding="utf-8") as arquivo:
            for linha in arquivo:
                try:
                    item = json.loads(linha)
                except ValueError:
                    continue
                if item.get("tipo") == "evento":
                    recentes.append(item)
    except FileNotFoundError:
        return []
    except OSError:
        log.exception("não foi possível ler o diário %s", ARQUIVO)
        return []
    return list(reversed(recentes))


def snapshot_catalogo() -> None:
    """Guarda o conteúdo atual dos arquivos de catálogo e de configuração (produtos, vitrines, modelos, padrões)."""
    arquivos = sorted(_CATALOGO.rglob("*.json")) + [
        Path(settings.BASE_DIR) / "ghost" / "config" / "eventos.json",
        Path(settings.BASE_DIR) / "ghost" / "config" / "config.json",
    ]
    quando = _agora()
    linhas = []
    for arquivo in arquivos:
        try:
            conteudo = json.loads(arquivo.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log.exception("não foi possível ler %s para o diário", arquivo)
            continue
        linhas.append(
            {
                "quando": quando,
                "tipo": "snapshot",
                "arquivo": arquivo.name,
                "conteudo": conteudo,
            }
        )
    _gravar(linhas)
