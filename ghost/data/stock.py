"""Estoque de contas e links: contagem, reserva, confirmação e liberação."""

from datetime import datetime, timedelta

from ..core import cache
from .. import catalogo
from ..config import settings
from ..database.connection import connection

_CONTA = "estoque"
_LINK = "estoque_links"


def _table(produto_key: str) -> str:
    return _LINK if is_link(produto_key) else _CONTA


def is_link(produto_key: str) -> bool:
    produto = catalogo.produto(produto_key)
    if produto is not None:
        return produto.modelo.estoque == "link"
    return produto_key.endswith("_link")


def count_global(produto_key: str) -> int:
    key = f"estoque:{produto_key}"
    cached = cache.estoque_cache.get(key)
    if cached is not None:
        return cached
    table = _table(produto_key)
    with connection() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) AS c FROM {table} WHERE produto_key=? AND vendido=0 AND reservado=0",
            (produto_key,),
        ).fetchone()["c"]
    cache.estoque_cache.set(key, total)
    return total


def count_for_user(produto_key: str, user_id: int) -> int:
    if is_link(produto_key):
        return count_global(produto_key)
    with connection() as conn:
        return conn.execute(
            """SELECT COUNT(*) AS c FROM estoque
               WHERE produto_key=? AND vendido=0 AND reservado=0
                 AND id NOT IN (SELECT estoque_id FROM entregas_confirmadas
                                WHERE discord_user_id=? AND produto_key=?)""",
            (produto_key, str(user_id), produto_key),
        ).fetchone()["c"]


def reserve(produto_key: str, user_id: int) -> dict | None:
    """Reserva um item para a compra. Deve ser confirmado ou liberado depois."""
    agora = datetime.now().isoformat()
    with connection() as conn:
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
        if is_link(produto_key):
            row = conn.execute(
                "SELECT id, link, extra FROM estoque_links WHERE produto_key=? AND vendido=0 AND reservado=0 "
                "ORDER BY RANDOM() LIMIT 1",
                (produto_key,),
            ).fetchone()
            if not row:
                return None
            conn.execute(
                "UPDATE estoque_links SET vendido=1, reservado=1, reservado_em=?, reservado_por=? WHERE id=?",
                (agora, str(user_id), row["id"]),
            )
            item = {
                "id": row["id"],
                "kind": "link",
                "link": row["link"],
                "extra": row["extra"],
            }
        else:
            row = conn.execute(
                """SELECT id, email, senha, extra FROM estoque
                   WHERE produto_key=? AND vendido=0 AND reservado=0
                     AND id NOT IN (SELECT estoque_id FROM entregas_confirmadas
                                    WHERE discord_user_id=? AND produto_key=?)
                   ORDER BY RANDOM() LIMIT 1""",
                (produto_key, str(user_id), produto_key),
            ).fetchone()
            if not row:
                return None
            conn.execute(
                "UPDATE estoque SET vendido=1, reservado=1, reservado_em=?, reservado_por=? WHERE id=?",
                (agora, str(user_id), row["id"]),
            )
            item = {
                "id": row["id"],
                "kind": "conta",
                "email": row["email"],
                "senha": row["senha"],
                "extra": row["extra"],
            }
    cache.estoque_cache.clear()
    return item


def confirm(item: dict, user_id: int, produto_key: str) -> None:
    table = "estoque_links" if item["kind"] == "link" else "estoque"
    with connection() as conn:
        updated = conn.execute(
            f"UPDATE {table} SET reservado=0, reservado_em=NULL, reservado_por=NULL "
            "WHERE id=? AND produto_key=? AND vendido=1 AND reservado=1 AND reservado_por=?",
            (item["id"], produto_key, str(user_id)),
        )
        if updated.rowcount != 1:
            raise ValueError("reserva ausente ou pertencente a outro cliente")
        conn.execute(
            "INSERT INTO entregas_confirmadas (discord_user_id, estoque_id, produto_key) VALUES (?,?,?)",
            (str(user_id), item["id"], produto_key),
        )
    cache.estoque_cache.clear()


def release(item: dict) -> None:
    table = "estoque_links" if item["kind"] == "link" else "estoque"
    with connection() as conn:
        conn.execute(
            f"UPDATE {table} SET vendido=0, reservado=0, reservado_em=NULL, reservado_por=NULL WHERE id=? AND reservado=1",
            (item["id"],),
        )
    cache.estoque_cache.clear()


def release_stale(minutos: int = settings.RESERVA_MAX_MINUTOS) -> int:
    limite = (datetime.now() - timedelta(minutes=minutos)).isoformat()
    total = 0
    with connection() as conn:
        for table in ("estoque", "estoque_links"):
            cur = conn.execute(
                f"UPDATE {table} SET vendido=0, reservado=0, reservado_em=NULL, reservado_por=NULL "
                f"WHERE reservado=1 AND reservado_em IS NOT NULL AND reservado_em < ? "
                f"AND id NOT IN (SELECT estoque_id FROM pedido_entregas WHERE status='sending' AND kind=?)",
                (limite, "link" if table == _LINK else "conta"),
            )
            total += cur.rowcount
    if total:
        cache.estoque_cache.clear()
    return total


def add_accounts(produto_key: str, linhas: list[str]) -> int:
    itens = []
    for linha in linhas:
        if ":" not in linha:
            continue
        email, senha = linha.split(":", 1)
        itens.append((produto_key, email.strip(), senha.strip()))
    if not itens:
        return 0
    with connection() as conn:
        conn.executemany(
            "INSERT INTO estoque (produto_key, email, senha) VALUES (?,?,?)", itens
        )
    cache.estoque_cache.clear()
    return len(itens)


def add_contas_detalhadas(produto_key: str, itens: list[dict]) -> int:
    """Itens do JSON de estoque: os campos escritos, guardados inteiros em `extra`. Email e senha são os dois
    primeiros campos (sem contar Adicional), para listar e remover como antes."""
    import json

    linhas = []
    for campos in itens:
        valores = [
            str(v)
            for nome, v in campos.items()
            if nome.strip().lower() not in ("adicional", "imagem", "miniatura")
        ]
        email = valores[0] if valores else ""
        senha = valores[1] if len(valores) > 1 else ""
        linhas.append(
            (produto_key, email, senha, json.dumps(campos, ensure_ascii=False))
        )
    if not linhas:
        return 0
    with connection() as conn:
        conn.executemany(
            "INSERT INTO estoque (produto_key, email, senha, extra) VALUES (?,?,?,?)",
            linhas,
        )
    cache.estoque_cache.clear()
    return len(itens)


def add_links(produto_key: str, links: list) -> int:
    """Aceita links em texto ou itens do JSON (dict de campos). O link é o primeiro campo; o item inteiro vai em extra."""
    import json

    linhas = []
    for item in links:
        campos = {"Link": item} if isinstance(item, str) else item
        valores = [
            str(v)
            for nome, v in campos.items()
            if nome.strip().lower() not in ("adicional", "imagem", "miniatura")
        ]
        if not valores:
            continue
        linhas.append(
            (produto_key, valores[0].strip(), json.dumps(campos, ensure_ascii=False))
        )
    if not linhas:
        return 0
    with connection() as conn:
        conn.executemany(
            "INSERT INTO estoque_links (produto_key, link, extra) VALUES (?,?,?)",
            linhas,
        )
    cache.estoque_cache.clear()
    return len(linhas)


def remove(produto_key: str, identificador: str) -> None:
    with connection() as conn:
        if is_link(produto_key):
            conn.execute(
                "DELETE FROM estoque_links WHERE produto_key=? AND link=? AND vendido=0 AND reservado=0",
                (produto_key, identificador),
            )
        else:
            conn.execute(
                "DELETE FROM estoque WHERE produto_key=? AND email=? AND vendido=0 AND reservado=0",
                (produto_key, identificador),
            )
    cache.estoque_cache.clear()


def clear(produto_key: str, *, link: bool | None = None) -> None:
    """Apaga todo o estoque do produto. `link` diz qual tabela usar, para quando o produto já saiu do catálogo."""
    tabela = _LINK if (is_link(produto_key) if link is None else link) else _CONTA
    with connection() as conn:
        conn.execute(
            f"DELETE FROM {tabela} WHERE produto_key=? AND reservado=0", (produto_key,)
        )
    cache.estoque_cache.clear()


def item_disponivel(produto_key: str, item_id: int) -> dict | None:
    """Um item ainda disponível (não vendido e não reservado), para editar ou apagar."""
    colunas, tabela = (
        ("id, link, extra", "estoque_links")
        if is_link(produto_key)
        else ("id, email, senha, extra", "estoque")
    )
    with connection() as conn:
        row = conn.execute(
            f"SELECT {colunas} FROM {tabela} WHERE id=? AND produto_key=? AND vendido=0 "
            "AND reservado=0",
            (item_id, produto_key),
        ).fetchone()
    return dict(row) if row else None


def substituir_item(produto_key: str, item_id: int, campos: dict) -> bool:
    """Troca os campos de um item disponível. Devolve False se ele já foi vendido ou está reservado."""
    import json

    extra = json.dumps(campos, ensure_ascii=False)
    valores = [
        str(v)
        for nome, v in campos.items()
        if nome.strip().lower() not in ("adicional", "imagem", "miniatura")
    ]
    with connection() as conn:
        if is_link(produto_key):
            cur = conn.execute(
                "UPDATE estoque_links SET link=?, extra=? WHERE id=? AND produto_key=? "
                "AND vendido=0 AND reservado=0",
                (valores[0].strip(), extra, item_id, produto_key),
            )
        else:
            cur = conn.execute(
                "UPDATE estoque SET email=?, senha=?, extra=? WHERE id=? AND produto_key=? "
                "AND vendido=0 AND reservado=0",
                (
                    valores[0],
                    valores[1] if len(valores) > 1 else "",
                    extra,
                    item_id,
                    produto_key,
                ),
            )
        ok = cur.rowcount == 1
    if ok:
        cache.estoque_cache.clear()
    return ok


def apagar_item(produto_key: str, item_id: int) -> bool:
    """Apaga um item disponível. Devolve False se ele já foi vendido ou está reservado."""
    tabela = "estoque_links" if is_link(produto_key) else "estoque"
    with connection() as conn:
        cur = conn.execute(
            f"DELETE FROM {tabela} WHERE id=? AND produto_key=? AND vendido=0 AND reservado=0",
            (item_id, produto_key),
        )
        ok = cur.rowcount == 1
    if ok:
        cache.estoque_cache.clear()
    return ok
