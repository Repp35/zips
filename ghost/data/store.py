"""Configuração da loja, preços, vitrines, threads, equipe, agendamentos e pedidos de restock."""

import json
import logging

from ..core import cache
from .. import catalogo
from ..config import settings
from ..database.connection import connection

TEAM_TABLES = {"admin": "admins", "staff": "staff"}


# ── Configuração ──────────────────────────────────────────────────────────────


def get_config(chave: str, default: str | None = None) -> str | None:
    with connection() as conn:
        row = conn.execute(
            "SELECT valor FROM config WHERE chave=?", (chave,)
        ).fetchone()
    return row["valor"] if row else default


def todas_configs() -> dict:
    with connection() as conn:
        return {
            r["chave"]: r["valor"]
            for r in conn.execute("SELECT chave, valor FROM config ORDER BY chave")
        }


def set_config(chave: str, valor: str) -> None:
    with connection() as conn:
        conn.execute(
            "INSERT INTO config (chave, valor) VALUES (?,?) "
            "ON CONFLICT(chave) DO UPDATE SET valor=excluded.valor",
            (chave, valor),
        )


def reset_config(chave: str) -> None:
    """Apaga o valor do banco: volta a valer o padrão do config.json (ou do .env)."""
    with connection() as conn:
        conn.execute("DELETE FROM config WHERE chave=?", (chave,))


def ajuste(chave: str) -> str | None:
    """Valor do banco tem a palavra final; se ele não existir, o padrão do config.json (ou None)."""
    valor = get_config(chave)
    if valor is None:
        valor = settings.EDITAVEIS.get(chave)
    if isinstance(valor, bool):
        return "1" if valor else "0"
    return None if valor is None else str(valor)


def config_atual(chave: str) -> tuple[str | None, str]:
    """Valor efetivo e de onde ele vem: banco, config.json, .env (só canais) ou nada."""
    banco = get_config(chave)
    if banco is not None:
        return banco, "banco"
    padrao = settings.EDITAVEIS.get(chave)
    if padrao is not None:
        if isinstance(padrao, bool):
            return ("1" if padrao else "0"), "config.json"
        return str(padrao), "config.json"
    if chave.endswith("_channel_id"):
        env = (
            getattr(settings, f"{chave[: -len('_channel_id')].upper()}_CHANNEL_ID", 0)
            or 0
        )
        if env:
            return str(env), ".env"
    return None, "não definido"


def niveis_cargo() -> list[dict]:
    """Níveis de cargo por valor comprado. O banco sobrepõe o config.json (chave cargos_niveis)."""
    bruto = get_config("cargos_niveis")
    try:
        niveis = json.loads(bruto) if bruto else settings.CARGOS_NIVEIS
    except ValueError:
        logging.getLogger("store").warning(
            "cargos_niveis inválido no banco; usando o padrão"
        )
        niveis = settings.CARGOS_NIVEIS
    return sorted(niveis, key=lambda n: float(n["valor"]))


def salvar_niveis(niveis: list[dict]) -> None:
    set_config("cargos_niveis", json.dumps(niveis, ensure_ascii=False))


def channel_for(tipo: str) -> int:
    value = ajuste(f"{tipo.lower()}_channel_id")
    if value and value.isdigit() and int(value) > 0:
        return int(value)
    return int(getattr(settings, f"{tipo.upper()}_CHANNEL_ID", 0) or 0)


# ── Preços ────────────────────────────────────────────────────────────────────


def produto_nome(produto_key: str) -> str:
    produto = catalogo.produto(produto_key)
    return produto.nome if produto else produto_key


def preco(produto_key: str) -> float:
    """Preço vem do arquivo do produto (catalogo/produtos/<id>.json)."""
    produto = catalogo.produto(produto_key)
    return produto.preco if produto else 0.0


# ── Catálogo: a fonte da verdade são os arquivos em catalogo/produtos/ (o banco não guarda produto) ──


def recarregar_catalogo() -> list[str]:
    """Relê os arquivos de produto e monta o catálogo. Devolve os erros; vazio quando deu certo.
    Se der erro, o catálogo anterior continua valendo."""
    try:
        catalogo.recarregar()
    except catalogo.CatalogoInvalido as exc:
        return [str(exc)]
    return []


# ── Vitrines (mensagens publicadas) ───────────────────────────────────────────


def setup_message(tipo: str):
    with connection() as conn:
        return conn.execute(
            "SELECT channel_id, message_id FROM setup_messages WHERE tipo=?", (tipo,)
        ).fetchone()


def save_setup_message(tipo: str, channel_id: int, message_id: int) -> None:
    with connection() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO setup_messages (tipo, channel_id, message_id) VALUES (?,?,?)",
            (tipo, str(channel_id), str(message_id)),
        )


def delete_setup_message(tipo: str) -> None:
    with connection() as conn:
        conn.execute("DELETE FROM setup_messages WHERE tipo=?", (tipo,))


def vitrine_assinatura(tipo: str) -> str | None:
    """Assinatura (hash) do conteúdo que está publicado. Serve para não reenviar vitrine sem mudança."""
    with connection() as conn:
        row = conn.execute(
            "SELECT assinatura FROM vitrines_assinatura WHERE tipo=?", (tipo,)
        ).fetchone()
    return row["assinatura"] if row else None


def save_vitrine_assinatura(tipo: str, assinatura: str) -> None:
    with connection() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO vitrines_assinatura (tipo, assinatura) VALUES (?,?)",
            (tipo, assinatura),
        )


# ── Threads de atendimento / carrinho ─────────────────────────────────────────


def register_thread(
    thread_id: int, user_id: int, tipo: str, produto_key: str | None = None
) -> None:
    with connection() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO threads_ativas (thread_id, user_id, tipo, produto_key) "
            "VALUES (?,?,?,?)",
            (str(thread_id), str(user_id), tipo, produto_key),
        )


def forget_thread(thread_id: int | str) -> None:
    with connection() as conn:
        conn.execute("DELETE FROM threads_ativas WHERE thread_id=?", (str(thread_id),))
        conn.execute(
            "DELETE FROM fechamentos_agendados WHERE thread_id=?", (str(thread_id),)
        )


def open_thread(user_id: int, tipo: str, produto_key: str | None = None):
    with connection() as conn:
        if produto_key is None:
            return conn.execute(
                "SELECT thread_id FROM threads_ativas WHERE user_id=? AND tipo=?",
                (str(user_id), tipo),
            ).fetchone()
        return conn.execute(
            "SELECT thread_id FROM threads_ativas WHERE user_id=? AND tipo=? AND produto_key=?",
            (str(user_id), tipo, produto_key),
        ).fetchone()


def thread_owner(thread_id: int | str):
    with connection() as conn:
        row = conn.execute(
            "SELECT user_id FROM threads_ativas WHERE thread_id=?", (str(thread_id),)
        ).fetchone()
    return row["user_id"] if row else None


def ticket_state(thread_id: int | str):
    with connection() as conn:
        return conn.execute(
            "SELECT user_id, tipo, ticket_escolha, assumido_por FROM threads_ativas WHERE thread_id=?",
            (str(thread_id),),
        ).fetchone()


def choose_ticket_reason(thread_id: int | str, escolha: str) -> bool:
    """Grava o motivo escolhido uma única vez. False se já havia escolha."""
    with connection() as conn:
        cur = conn.execute(
            "UPDATE threads_ativas SET ticket_escolha=? WHERE thread_id=? AND ticket_escolha IS NULL",
            (escolha, str(thread_id)),
        )
        return cur.rowcount == 1


def claim_ticket(thread_id: int | str, admin_id: int) -> bool:
    """Atribui o ticket a um admin uma única vez. False se já estava assumido."""
    with connection() as conn:
        cur = conn.execute(
            "UPDATE threads_ativas SET assumido_por=? WHERE thread_id=? AND assumido_por IS NULL",
            (str(admin_id), str(thread_id)),
        )
        return cur.rowcount == 1


def mark_manual(thread_id: int | str) -> None:
    with connection() as conn:
        conn.execute(
            "UPDATE threads_ativas SET entrega_manual=1 WHERE thread_id=?",
            (str(thread_id),),
        )


def is_banned(user_id: int) -> bool:
    with connection() as conn:
        return bool(
            conn.execute(
                "SELECT 1 FROM banidos WHERE user_id=?", (str(user_id),)
            ).fetchone()
        )


# ── Equipe ────────────────────────────────────────────────────────────────────


def _equipe() -> dict[str, set[int]]:
    cached = cache.equipe_cache.get("equipe")
    if cached is None:
        with connection() as conn:
            cached = {
                tipo: {
                    int(r["user_id"])
                    for r in conn.execute(f"SELECT user_id FROM {table}")
                }
                for tipo, table in TEAM_TABLES.items()
            }
        cache.equipe_cache.set("equipe", cached)
    return cached


def listar_equipe(tipo: str) -> list[int]:
    """Admins. A tabela staff também conta como admin: só existem dois níveis, dono e admin."""
    tabelas = ["admins", "staff"] if tipo == "admin" else [TEAM_TABLES[tipo]]
    ids: list[int] = []
    with connection() as conn:
        for table in tabelas:
            ids += [
                int(r["user_id"])
                for r in conn.execute(
                    f"SELECT user_id FROM {table} ORDER BY adicionado_em"
                )
            ]
    return ids


def is_admin(user_id: int) -> bool:
    return (
        user_id == settings.ADMIN_ID
        or user_id in _equipe().get("admin", ())
        or user_id in _equipe().get("staff", ())
    )


def is_staff(user_id: int) -> bool:
    """Staff não existe mais como nível: é o mesmo que admin."""
    return is_admin(user_id)


def team_ids() -> set[int]:
    ids = {settings.ADMIN_ID}
    for membros in _equipe().values():
        ids |= membros
    return ids


def set_team_member(tipo: str, user_id: str, by: int | None, add: bool) -> None:
    table = TEAM_TABLES[tipo]
    with connection() as conn:
        if add:
            conn.execute(
                f"INSERT OR REPLACE INTO {table} (user_id, adicionado_por, adicionado_em) "
                "VALUES (?,?,datetime('now'))",
                (user_id, str(by)),
            )
        else:
            conn.execute(f"DELETE FROM {table} WHERE user_id=?", (user_id,))
            if tipo == "admin":
                conn.execute("DELETE FROM staff WHERE user_id=?", (user_id,))
    cache.equipe_cache.clear()


# ── Webhooks (anti-raid) ──────────────────────────────────────────────────────


def save_webhook_creator(webhook_id: str, criador_id: int) -> None:
    with connection() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO webhooks_criadores (webhook_id, criador_id, criado_em) "
            "VALUES (?,?,datetime('now'))",
            (str(webhook_id), str(criador_id)),
        )


def webhook_creator(webhook_id: str) -> int | None:
    with connection() as conn:
        row = conn.execute(
            "SELECT criador_id FROM webhooks_criadores WHERE webhook_id=?",
            (str(webhook_id),),
        ).fetchone()
    return int(row["criador_id"]) if row else None


def save_bot_adder(bot_id: int, adicionado_por: int) -> None:
    with connection() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO bots_adicionados (bot_id, adicionado_por, adicionado_em) "
            "VALUES (?,?,datetime('now'))",
            (str(bot_id), str(adicionado_por)),
        )


def bot_adder(bot_id: int) -> int | None:
    with connection() as conn:
        row = conn.execute(
            "SELECT adicionado_por FROM bots_adicionados WHERE bot_id=?", (str(bot_id),)
        ).fetchone()
    return int(row["adicionado_por"]) if row else None


def forget_webhook(webhook_id: str) -> None:
    with connection() as conn:
        conn.execute(
            "DELETE FROM webhooks_criadores WHERE webhook_id=?", (str(webhook_id),)
        )


# ── Snapshot de canais (anti-nuke) ────────────────────────────────────────────


def pop_channel_snapshot(channel_id: int):
    with connection() as conn:
        row = conn.execute(
            "SELECT * FROM canais_snapshot WHERE channel_id=?", (str(channel_id),)
        ).fetchone()
        conn.execute(
            "DELETE FROM canais_snapshot WHERE channel_id=?", (str(channel_id),)
        )
    return row


def snapshot_ids() -> set[str]:
    with connection() as conn:
        return {
            r["channel_id"]
            for r in conn.execute("SELECT channel_id FROM canais_snapshot")
        }


def save_channel_snapshots(linhas: list[tuple]) -> None:
    """Grava vários snapshots em uma única transação."""
    with connection() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO canais_snapshot (channel_id, name, type, category_id, topic, position, permissoes, atualizado_em) "
            "VALUES (?,?,?,?,?,?,?,datetime('now'))",
            linhas,
        )


def drop_snapshots(ids: set[str]) -> None:
    if not ids:
        return
    with connection() as conn:
        conn.executemany(
            "DELETE FROM canais_snapshot WHERE channel_id=?", [(cid,) for cid in ids]
        )


# ── Fechamentos agendados ─────────────────────────────────────────────────────


def schedule_close(thread_id: int | str, fechar_em_iso: str, motivo: str) -> int:
    with connection() as conn:
        conn.execute(
            "DELETE FROM fechamentos_agendados WHERE thread_id=?", (str(thread_id),)
        )
        cur = conn.execute(
            "INSERT INTO fechamentos_agendados (thread_id, fechar_em, motivo) VALUES (?,?,?)",
            (str(thread_id), fechar_em_iso, motivo),
        )
        return cur.lastrowid


def delete_schedule(schedule_id: int) -> None:
    with connection() as conn:
        conn.execute("DELETE FROM fechamentos_agendados WHERE id=?", (schedule_id,))


def cancel_schedules(thread_id: int | str) -> None:
    with connection() as conn:
        conn.execute(
            "DELETE FROM fechamentos_agendados WHERE thread_id=?", (str(thread_id),)
        )


def pending_schedules():
    with connection() as conn:
        return conn.execute(
            "SELECT id, thread_id, fechar_em, motivo FROM fechamentos_agendados"
        ).fetchall()


# ── Restock ───────────────────────────────────────────────────────────────────


def restock_pending(user_id: int, produto_key: str):
    with connection() as conn:
        return conn.execute(
            "SELECT thread_id FROM restock_pedidos WHERE user_id=? AND produto_key=? AND status='pendente'",
            (str(user_id), produto_key),
        ).fetchone()


def restock_pending_last_day() -> int:
    with connection() as conn:
        return conn.execute(
            "SELECT COUNT(*) AS c FROM restock_pedidos WHERE status='pendente' "
            "AND criado_em >= datetime('now', '-1 day')"
        ).fetchone()["c"]


def create_restock(user_id: int, produto_key: str, thread_id: int) -> None:
    """Lança sqlite3.IntegrityError se já houver pedido pendente para o par."""
    with connection() as conn:
        conn.execute(
            "INSERT INTO restock_pedidos (user_id, produto_key, thread_id, status) "
            "VALUES (?,?,?,'pendente')",
            (str(user_id), produto_key, str(thread_id)),
        )


def delete_restock_pending(user_id: int, produto_key: str) -> None:
    with connection() as conn:
        conn.execute(
            "DELETE FROM restock_pedidos WHERE user_id=? AND produto_key=? AND status='pendente'",
            (str(user_id), produto_key),
        )


def close_restock_thread(thread_id: int | str) -> None:
    """Fecha um pedido de restock pelo id da thread: o cliente pode pedir de novo depois."""
    with connection() as conn:
        conn.execute(
            "DELETE FROM restock_pedidos WHERE thread_id=? AND status='pendente'",
            (str(thread_id),),
        )


def conclude_restock(produto_key: str):
    with connection() as conn:
        rows = conn.execute(
            "SELECT thread_id FROM restock_pedidos WHERE produto_key=? AND status='pendente'",
            (produto_key,),
        ).fetchall()
        conn.execute(
            "UPDATE restock_pedidos SET status='concluido' WHERE produto_key=? AND status='pendente'",
            (produto_key,),
        )
    return [r["thread_id"] for r in rows]


# ── Avisos de estoque ("me avise quando chegar") ─────────────────────────────


def inscrever_aviso(user_id: int, produto_key: str) -> bool:
    """Registra o aviso. Retorna False se o usuário já estava inscrito."""
    with connection() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO avisos_estoque (user_id, produto_key) VALUES (?,?)",
            (str(user_id), produto_key),
        )
        return cur.rowcount == 1


def avisos_do_produto(produto_key: str) -> list[int]:
    with connection() as conn:
        return [
            int(r["user_id"])
            for r in conn.execute(
                "SELECT user_id FROM avisos_estoque WHERE produto_key=?", (produto_key,)
            )
        ]


def remover_aviso(user_id: int, produto_key: str) -> None:
    with connection() as conn:
        conn.execute(
            "DELETE FROM avisos_estoque WHERE user_id=? AND produto_key=?",
            (str(user_id), produto_key),
        )


# ── Novidades: produtos que já foram anunciados no canal de avisos ───────────


def produtos_anunciados() -> set[str]:
    with connection() as conn:
        return {
            r["produto_key"]
            for r in conn.execute("SELECT produto_key FROM produtos_anunciados")
        }


def marcar_anunciado(produto_key: str) -> None:
    with connection() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO produtos_anunciados (produto_key) VALUES (?)",
            (produto_key,),
        )
