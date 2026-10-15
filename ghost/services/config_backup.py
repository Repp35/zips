"""Backup diário das configurações: produtos (JSON), config.json e as configurações guardadas no banco.

- Um arquivo por dia, e só se algo mudou desde o último arquivo. Sem mudança, não grava nada.
- Credenciais (.env, tokens) nunca entram aqui.
- Ficam os últimos MANTER arquivos em backups/config/.
"""

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .. import catalogo
from ..config import settings
from ..core.files import atomic_text
from ..data import store

log = logging.getLogger("config_backup")

PASTA: Path = settings.BASE_DIR / "backups" / "config"
MANTER = 30
FUSO = timezone(timedelta(hours=-3))  # Belém


def _estado() -> dict:
    produtos = {
        p.id: catalogo.dados_brutos(p.id) for p in catalogo.produtos(so_ativos=False)
    }
    try:
        config_json = json.loads(
            (settings.BASE_DIR / "ghost" / "config" / "config.json").read_text(
                encoding="utf-8"
            )
        )
    except (OSError, ValueError):
        config_json = None
    return {
        "produtos": produtos,
        "config_json": config_json,
        "banco": store.todas_configs(),
    }


def _assinatura(estado: dict) -> str:
    return hashlib.sha256(
        json.dumps(estado, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _arquivos() -> list[Path]:
    return sorted(PASTA.glob("config-*.json")) if PASTA.exists() else []


def _ultimo() -> dict | None:
    arqs = _arquivos()
    if not arqs:
        return None
    try:
        return json.loads(arqs[-1].read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def diario() -> Path | None:
    """Grava o estado de hoje se ainda não há arquivo de hoje e se algo mudou desde o último arquivo."""
    from . import backup
    if backup.em_raid() or backup.estado_restauracao().get("status") in ("rodando", "aguardando", "erro", "parado"):
        return None
    hoje = datetime.now(FUSO).strftime("%Y-%m-%d")
    if any(a.name.startswith(f"config-{hoje}") for a in _arquivos()):
        return None
    estado = _estado()
    anterior = _ultimo()
    if anterior is not None and _assinatura(anterior["estado"]) == _assinatura(estado):
        return None
    PASTA.mkdir(parents=True, exist_ok=True)
    caminho = PASTA / f"config-{datetime.now(FUSO):%Y-%m-%d-%H%M%S}.json"
    atomic_text(
        caminho,
        json.dumps(
            {
                "criado_em": datetime.now(FUSO).isoformat(timespec="seconds"),
                "estado": estado,
            },
            ensure_ascii=False,
            indent=2,
        ),
    )
    for antigo in _arquivos()[:-MANTER]:
        antigo.unlink(missing_ok=True)
    log.info("backup de configurações gravado: %s", caminho.name)
    return caminho
