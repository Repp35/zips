"""Diário das ações do Ghost Agent. Cada ação feita por plano aprovado guarda como desfazer.

- Só ações de plano entram no diário (leituras não entram).
- Desfazer nunca roda sozinho: a IA só monta um plano com `desfazer_acao`, e o dono aprova.
- Ação sem volta (ex.: mensagens apagadas) fica marcada como irreversível, com o motivo.
"""

import contextvars
import json
from datetime import datetime, timedelta, timezone

from ..database.connection import connection

FUSO = timezone(timedelta(hours=-3))

_SLOT: contextvars.ContextVar = contextvars.ContextVar("diario_slot", default=None)


# ── captura durante a execução ────────────────────────────────────────────────


def iniciar() -> dict:
    """Abre um espaço para a ferramenta informar como desfazer. Chamado antes de cada ação de plano."""
    slot: dict = {}
    _SLOT.set(slot)
    return slot


def informar(desfazer: dict | None = None, motivo: str | None = None) -> None:
    """Chamado pela ferramenta depois que a alteração deu certo. `desfazer` é a receita de volta;
    `motivo` explica por que não tem volta."""
    slot = _SLOT.get()
    if slot is not None:
        slot["desfazer"] = desfazer
        slot["motivo"] = motivo


# ── gravação ──────────────────────────────────────────────────────────────────


def gravar(ferramenta: str, resumo: str, plano: str, dono_id: int, info: dict) -> int:
    desfazer = info.get("desfazer")
    motivo = info.get("motivo")
    if desfazer:
        status = "feito"
    elif motivo:
        status = "irreversivel"
    else:
        status = "sem_efeito"
    agora = datetime.now(FUSO).strftime("%d/%m/%Y %H:%M:%S")
    with connection() as conn:
        cur = conn.execute(
            "INSERT INTO agente_diario (criado_em, ferramenta, resumo, plano, dono, desfazer, motivo, "
            "status) VALUES (?,?,?,?,?,?,?,?)",
            (
                agora,
                ferramenta,
                resumo[:200],
                plano,
                str(dono_id),
                json.dumps(desfazer, ensure_ascii=False) if desfazer else None,
                motivo,
                status,
            ),
        )
        return cur.lastrowid


def obter(entrada_id: int) -> dict | None:
    with connection() as conn:
        linha = conn.execute(
            "SELECT * FROM agente_diario WHERE id=?", (entrada_id,)
        ).fetchone()
    return dict(linha) if linha else None


def ultimas(limite: int = 15) -> list[dict]:
    with connection() as conn:
        linhas = conn.execute(
            "SELECT * FROM agente_diario ORDER BY id DESC LIMIT ?", (limite,)
        ).fetchall()
    return [dict(l) for l in linhas]


def _marcar(entrada_id: int, status: str) -> None:
    with connection() as conn:
        conn.execute(
            "UPDATE agente_diario SET status=? WHERE id=?", (status, entrada_id)
        )


def conferir_desfazer(args: dict) -> str | None:
    """Checagem antes de entrar no plano: a ação existe e ainda pode ser desfeita?"""
    try:
        entrada_id = int(args.get("id"))
    except (TypeError, ValueError):
        return "informe o número da ação no diário"
    entrada = obter(entrada_id)
    if entrada is None:
        return f"não existe ação #{entrada_id} no diário"
    if entrada["status"] != "feito":
        motivo = entrada["motivo"] or "essa ação não alterou nada"
        return f"a ação #{entrada_id} não pode ser desfeita ({motivo})"
    return None


def rotulo(entrada: dict) -> str:
    marca = {
        "feito": "↩️ dá pra desfazer",
        "revertido": "✅ já desfeita",
        "irreversivel": "⛔ sem volta",
        "sem_efeito": "· sem efeito",
    }.get(entrada["status"], entrada["status"])
    extra = (
        f" ({entrada['motivo']})"
        if entrada.get("motivo") and entrada["status"] == "irreversivel"
        else ""
    )
    return f"`#{entrada['id']}` {entrada['criado_em']} · {entrada['resumo']} · {marca}{extra}"


# ── snapshot de estoque (para desfazer um esvaziamento) ──────────────────────


def estoque_atual(produto_id: str) -> list[dict]:
    from ..data import stock

    table = "estoque_links" if stock.is_link(produto_id) else "estoque"
    with connection() as conn:
        return [
            dict(r)
            for r in conn.execute(
                f"SELECT * FROM {table} WHERE produto_key=? AND reservado=0",
                (produto_id,),
            )
        ]


# ── reversão (só roda com o plano aprovado pelo dono) ────────────────────────


async def reverter(guild, entrada_id: int, dono) -> str:
    from ..bot.cogs import comandos_acoes
    from ..data import coupons, stock
    from ..services import produtos, shelves

    entrada = obter(entrada_id)
    if entrada is None:
        raise ValueError(f"não existe ação #{entrada_id} no diário")
    if entrada["status"] != "feito":
        raise ValueError(f"a ação #{entrada_id} não pode ser desfeita")
    d = json.loads(entrada["desfazer"])
    acao = d["acao"]

    def canal(cid):
        ch = guild.get_channel(int(cid)) if cid else None
        if ch is None:
            raise ValueError("o canal ou categoria não existe mais")
        return ch

    if acao == "apagar_canal_id":
        ch = canal(d["id"])
        await ch.delete(reason="Ghost Agent: desfazer")
        texto = f"{ch.name} apagado."
    elif acao == "recriar_canal_seguro":
        from ..services import backup

        snapshot = d["snapshot"]
        feats = await backup._canais(guild, {"canais": [snapshot]}, {})
        if not feats or any(f.startswith("falha") for f in feats):
            raise ValueError("canal não pôde ser recriado com segurança")
        texto = "; ".join(feats)
    elif acao == "recriar_canal":
        categoria = (
            guild.get_channel(int(d["categoria_id"])) if d.get("categoria_id") else None
        )
        if d.get("tipo") == "voz":
            ch = await guild.create_voice_channel(
                d["nome"], category=categoria, reason="Ghost Agent: desfazer"
            )
        else:
            ch = await guild.create_text_channel(
                d["nome"], category=categoria, reason="Ghost Agent: desfazer"
            )
        texto = f"Canal {ch.name} recriado. As mensagens antigas não voltam sozinhas (use /backup restaurar)."
    elif acao == "renomear":
        ch = canal(d["id"])
        await ch.edit(name=d["nome"], reason="Ghost Agent: desfazer")
        texto = f"Canal voltou a se chamar {d['nome']}."
    elif acao == "renomear_servidor":
        await guild.edit(name=d["nome"], reason="Ghost Agent: desfazer")
        texto = f"Servidor voltou a se chamar {d['nome']}."
    elif acao == "travar_id":
        texto = await comandos_acoes.travar(canal(d["id"]), guild, dono)
    elif acao == "destravar_id":
        texto = await comandos_acoes.destravar(canal(d["id"]), guild, dono)
    elif acao == "imagem_help":
        from . import ferramentas

        dados = json.loads(ferramentas.AJUDA.read_text(encoding="utf-8"))
        dados["url"] = d.get("url") or ""
        ferramentas.AJUDA.write_text(
            json.dumps(dados, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        texto = "Imagem do /help voltou ao que era."
    elif acao == "produto":
        if d.get("doc") is None:
            texto = (await produtos.apagar(d["id"], guild, dono.id)).texto()
        else:
            texto = (await produtos.cadastrar(d["doc"], guild, dono.id)).texto()
    elif acao == "cupom_apagar":
        coupons.apagar(int(d["id"]))
        texto = "Cupom apagado."
    elif acao == "cupom_estado":
        if coupons.get(int(d["id"])) is None:
            raise ValueError("cupom não existe mais")
        coupons.atualizar(int(d["id"]), ativo=int(d["ativo"]))
        texto = "Estado anterior do cupom restaurado."
    elif acao == "cupom_alternar":
        coupons.alternar(int(d["id"]))
        texto = "Cupom ligado/desligado de volta."
    elif acao == "setup":
        vitrine = comandos_acoes.vitrine_do_texto(d["vitrine"])
        if vitrine is None:
            raise ValueError("vitrine não existe mais")
        if d.get("canal_id") is None:
            linhas = await comandos_acoes.executar_setup(
                "remove", vitrine.id, None, None, dono
            )
        else:
            linhas = await comandos_acoes.executar_setup(
                "add", vitrine.id, canal(d["canal_id"]), None, dono
            )
        texto = comandos_acoes.juntar(linhas)
    elif acao == "estoque_remover_ids":
        from ..core import cache

        table = "estoque_links" if stock.is_link(d["produto"]) else "estoque"
        with connection() as conn:
            conn.executemany(
                f"DELETE FROM {table} WHERE id=? AND produto_key=? AND vendido=0 AND reservado=0",
                [(i, d["produto"]) for i in d["itens"]],
            )
        cache.estoque_cache.clear()
        await shelves.refresh_for_product(d["produto"])
        texto = "Itens ainda disponíveis da adição removidos. Vendidos e reservados preservados."
    elif acao == "estoque_remover":
        for ident in d["itens"]:
            stock.remove(d["produto"], ident)
        await shelves.refresh_for_product(d["produto"])
        texto = f"{len(d['itens'])} item(ns) tirado(s) do estoque de {d['produto']}."
    elif acao == "estoque_restaurar":
        from ..core import cache

        if any(not isinstance(row, dict) for row in d["itens"]):
            raise ValueError(
                "snapshot antigo não preserva vendas; restauração automática recusada"
            )
        table = "estoque_links" if stock.is_link(d["produto"]) else "estoque"
        with connection() as conn:
            allowed = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            for row in d["itens"]:
                columns = list(row)
                if set(columns) - allowed or row["produto_key"] != d["produto"]:
                    raise ValueError("snapshot de estoque inválido")
                existing = conn.execute(
                    f"SELECT * FROM {table} WHERE id=?", (row["id"],)
                ).fetchone()
                if existing is not None:
                    if dict(existing) != row:
                        raise ValueError("o estoque mudou; revisão manual necessária")
                    continue
                conn.execute(
                    f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                    tuple(row[c] for c in columns),
                )
        cache.estoque_cache.clear()
        await shelves.refresh_for_product(d["produto"])
        texto = f"Estoque de {d['produto']} restaurado preservando vendas e dados."
    else:
        raise ValueError(f"não sei desfazer '{acao}'")

    _marcar(entrada_id, "revertido")
    if d.get("aviso"):
        texto += f" {d['aviso']}"
    return texto
