"""Conversa do Ghost Agent guardada no banco, por canal. Sobrevive a reinício do bot.

- `mensagens`: o histórico que vai para o modelo (a API é sem estado: o histórico vai inteiro em cada chamada).
- `ligada`: se o agente está ligado no canal. Não some com o tempo; use `/agente controle` para encerrar.
- `/agente novo` apaga o histórico; o canal continua ligado.
"""

import json
from datetime import datetime

from ..database.connection import connection


def carregar(canal_id: int) -> list[dict]:
    with connection() as conn:
        linha = conn.execute("SELECT mensagens FROM agente_conversas WHERE canal_id=?", (canal_id,)).fetchone()
    if not linha:
        return []
    try:
        dados = json.loads(linha["mensagens"] or "[]")
    except ValueError:
        return []
    return dados if isinstance(dados, list) else []


def salvar(canal_id: int, mensagens: list[dict]) -> None:
    with connection() as conn:
        conn.execute(
            "INSERT INTO agente_conversas (canal_id, mensagens, atualizado_em) VALUES (?,?,?) "
            "ON CONFLICT(canal_id) DO UPDATE SET mensagens=excluded.mensagens, atualizado_em=excluded.atualizado_em",
            (canal_id, json.dumps(mensagens, ensure_ascii=False), datetime.now().isoformat()),
        )


def ligada(canal_id: int) -> bool:
    with connection() as conn:
        linha = conn.execute("SELECT ligada FROM agente_conversas WHERE canal_id=?", (canal_id,)).fetchone()
    return bool(linha and linha["ligada"])


def definir_ligada(canal_id: int, valor: bool) -> None:
    with connection() as conn:
        conn.execute(
            "INSERT INTO agente_conversas (canal_id, ligada, atualizado_em) VALUES (?,?,?) "
            "ON CONFLICT(canal_id) DO UPDATE SET ligada=excluded.ligada",
            (canal_id, 1 if valor else 0, datetime.now().isoformat()),
        )


def apagar_historico(canal_id: int) -> None:
    with connection() as conn:
        conn.execute("UPDATE agente_conversas SET mensagens='[]', atualizado_em=? WHERE canal_id=?",
                     (datetime.now().isoformat(), canal_id))
