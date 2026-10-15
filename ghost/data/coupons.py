"""Cupons de desconto: cadastro, regras de uso, validação e contadores.

Um cupom dá desconto em porcentagem (tipo_desconto='pct', desconto_pct) ou em valor (tipo_desconto='valor',
desconto_valor). As regras de quem pode usar ficam em colunas: cargos e usuários permitidos ou bloqueados
(listas de IDs em JSON), só primeira compra, compras aprovadas mínimas, limite de usos total e por pessoa,
validade e valor mínimo do produto.

O carrinho usa `avaliar` para saber se pode aplicar (devolve o motivo ou None) e `desconto` para o valor
a abater. Os contadores (usos_atuais e cupons_uso) são mantidos por register_use e refund_for_pedido.
"""

import json
import math
import re
from datetime import date, datetime, timezone, timedelta


def hoje():
    return datetime.now(timezone(timedelta(hours=-3))).date()


from ..database.connection import connection
LISTAS = (
    "cargos_permitidos",
    "cargos_bloqueados",
    "usuarios_permitidos",
    "usuarios_bloqueados",
)
CAMPOS_EDITAVEIS = {
    "nome",
    "desconto_pct",
    "desconto_valor",
    "tipo_desconto",
    "primeira_compra",
    "min_produto",
    "min_compras",
    "limite_usos",
    "limite_por_usuario",
    "validade",
    "ativo",
    *LISTAS,
}

# ── Leitura ───────────────────────────────────────────────────────────────────


def _ids(bruto) -> list[str]:
    if not bruto:
        return []
    lista = json.loads(bruto) if isinstance(bruto, str) else bruto
    return [str(item) for item in lista]


def _ids_json(lista) -> str | None:
    ids = sorted({str(item) for item in lista or []})
    return json.dumps(ids) if ids else None


def _normalizar(linha) -> dict:
    """Linha do banco com valores padrão para colunas que bancos antigos ainda não têm."""
    dados = dict(linha)
    dados.setdefault("tipo_desconto", "pct")
    dados.setdefault("desconto_valor", None)
    dados.setdefault("limite_por_usuario", None)
    dados.setdefault("min_compras", 0)
    for chave in LISTAS:
        dados[chave] = _ids(dados.get(chave))
    return dados


def get(nome_ou_id) -> dict | None:
    with connection() as conn:
        if isinstance(nome_ou_id, int):
            row = conn.execute(
                "SELECT * FROM cupons WHERE id=?", (int(nome_ou_id),)
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM cupons WHERE nome=? COLLATE NOCASE", (nome_ou_id,)
            ).fetchone()
    return _normalizar(row) if row else None


def listar() -> list[dict]:
    with connection() as conn:
        linhas = conn.execute(
            "SELECT * FROM cupons ORDER BY nome COLLATE NOCASE"
        ).fetchall()
    return [_normalizar(linha) for linha in linhas]


def id_by_name(nome: str) -> int | None:
    with connection() as conn:
        row = conn.execute(
            "SELECT id FROM cupons WHERE nome=? COLLATE NOCASE", (nome,)
        ).fetchone()
    return row["id"] if row else None


# ── Cadastro ──────────────────────────────────────────────────────────────────


def criar(
    nome: str,
    *,
    desconto_pct: int | None = None,
    desconto_valor: float | None = None,
    limite_usos: int | None = None,
    validade: str | None = None,
    min_produto: float = 0.0,
    limite_por_usuario: int | None = None,
    min_compras: int = 0,
    primeira_compra: bool = False,
) -> int:
    """Cria o cupom. Desconto em valor se `desconto_valor` vier preenchido, senão em porcentagem.
    As regras de cargo e de usuário entram depois, por `editar`."""
    nome = nome.strip().upper()
    if not re.fullmatch(r"[A-Z0-9_-]{1,20}", nome):
        raise ValueError("código inválido")
    if desconto_valor is not None:
        if (
            not math.isfinite(float(desconto_valor))
            or round(float(desconto_valor), 2) <= 0
            or desconto_pct is not None
        ):
            raise ValueError("informe um desconto válido de um só tipo")
    elif desconto_pct is None or not 1 <= desconto_pct <= 99:
        raise ValueError("porcentagem de 1 a 99")
    if not math.isfinite(min_produto) or min_produto < 0 or min_compras < 0:
        raise ValueError("mínimos inválidos")
    if any(n is not None and n < 1 for n in (limite_usos, limite_por_usuario)):
        raise ValueError("limites precisam ser positivos")
    if validade:
        validade = date.fromisoformat(validade).isoformat()
    tipo = "valor" if desconto_valor is not None else "pct"
    with connection() as conn:
        if conn.execute(
            "SELECT 1 FROM cupons WHERE nome=? COLLATE NOCASE", (nome,)
        ).fetchone():
            raise ValueError("Já existe um cupom com esse nome.")
        cur = conn.execute(
            "INSERT INTO cupons (nome, desconto_pct, desconto_valor, tipo_desconto, limite_usos, validade, "
            "min_produto, limite_por_usuario, min_compras, primeira_compra) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                nome,
                None if tipo == "valor" else desconto_pct,
                desconto_valor if tipo == "valor" else None,
                tipo,
                limite_usos,
                validade,
                min_produto,
                limite_por_usuario,
                min_compras,
                int(primeira_compra),
            ),
        )
        return cur.lastrowid


def atualizar(cupom_id: int, **campos) -> None:
    """Altera só os campos informados. Listas de IDs (cargos e usuários) são gravadas como JSON."""
    desconhecidos = set(campos) - CAMPOS_EDITAVEIS
    if desconhecidos:
        raise ValueError(
            f"campos de cupom desconhecidos: {', '.join(sorted(desconhecidos))}"
        )
    if not campos:
        return
    valores = [_ids_json(v) if k in LISTAS else v for k, v in campos.items()]
    sets = ", ".join(f"{k}=?" for k in campos)
    with connection() as conn:
        conn.execute(f"UPDATE cupons SET {sets} WHERE id=?", (*valores, cupom_id))


def editar(cupom_id: int, **campos) -> None:
    """Altera campos do cupom conferindo as regras: desconto válido, um só tipo de desconto por vez,
    datas no formato AAAA-MM-DD e limites positivos. Campo com None limpa o valor (sem limite, sem validade)."""
    if "desconto_pct" in campos and campos["desconto_pct"] is not None:
        if not 1 <= int(campos["desconto_pct"]) <= 99:
            raise ValueError("O desconto em porcentagem vai de 1 a 99.")
    if "desconto_valor" in campos and campos["desconto_valor"] is not None:
        if (
            not math.isfinite(float(campos["desconto_valor"]))
            or round(float(campos["desconto_valor"]), 2) <= 0
        ):
            raise ValueError("O desconto em valor precisa ser maior que zero.")
    tipo = campos.get("tipo_desconto")
    if tipo == "pct":
        campos.setdefault("desconto_valor", None)
    elif tipo == "valor":
        campos.setdefault("desconto_pct", None)
    if campos.get("validade"):
        try:
            date.fromisoformat(campos["validade"])
        except ValueError:
            raise ValueError(
                "A validade precisa estar no formato AAAA-MM-DD."
            ) from None
    for chave in ("limite_usos", "limite_por_usuario"):
        if campos.get(chave) is not None and int(campos[chave]) < 1:
            raise ValueError(f"{chave.replace('_', ' ')} precisa ser pelo menos 1.")
    if campos.get("min_compras") is not None and int(campos["min_compras"]) < 0:
        raise ValueError("O mínimo de compras não pode ser negativo.")
    # Colunas NOT NULL: vazio vira o padrão, não NULL.
    if "min_compras" in campos:
        campos["min_compras"] = int(campos["min_compras"] or 0)
    if "min_produto" in campos:
        campos["min_produto"] = float(campos["min_produto"] or 0.0)
        if not math.isfinite(campos["min_produto"]) or campos["min_produto"] < 0:
            raise ValueError("valor mínimo inválido")
    if "primeira_compra" in campos:
        campos["primeira_compra"] = int(bool(campos["primeira_compra"]))
    atualizar(cupom_id, **campos)


def alternar(cupom_id: int) -> bool:
    """Liga ou desliga o cupom. Devolve o novo estado."""
    with connection() as conn:
        conn.execute("UPDATE cupons SET ativo = 1 - ativo WHERE id=?", (cupom_id,))
        row = conn.execute(
            "SELECT ativo FROM cupons WHERE id=?", (cupom_id,)
        ).fetchone()
    return bool(row and row["ativo"])


def apagar(cupom_id: int) -> None:
    """Apaga o cupom e o registro dos usos dele (a tabela de usos tem chave estrangeira para o cupom)."""
    with connection() as conn:
        conn.execute("DELETE FROM cupons_uso WHERE cupom_id=?", (cupom_id,))
        conn.execute("DELETE FROM cupons WHERE id=?", (cupom_id,))


# ── Uso ───────────────────────────────────────────────────────────────────────


def register_use(cupom_id: int, user_id: int, pedido_id: int) -> None:
    with connection() as conn:
        insercao = conn.execute(
            "INSERT OR IGNORE INTO cupons_uso (cupom_id, user_id, pedido_id) VALUES (?,?,?)",
            (cupom_id, str(user_id), pedido_id),
        )
        if insercao.rowcount:
            conn.execute(
                "UPDATE cupons SET usos_atuais = usos_atuais + 1 WHERE id=?",
                (cupom_id,),
            )


def refund_for_pedido(pedido_id: int) -> None:
    with connection() as conn:
        conn.execute(
            """UPDATE cupons SET usos_atuais = MAX(0, usos_atuais - 1)
               WHERE id IN (SELECT cupom_id FROM cupons_uso WHERE pedido_id=?)""",
            (pedido_id,),
        )
        conn.execute("DELETE FROM cupons_uso WHERE pedido_id=?", (pedido_id,))


def _compras_aprovadas(user_id: int) -> int:
    with connection() as conn:
        return conn.execute(
            "SELECT COUNT(*) AS c FROM pedidos WHERE discord_user_id=? AND status='approved'",
            (str(user_id),),
        ).fetchone()["c"]


def _usos_da_pessoa(cupom_id: int, user_id: int) -> int:
    with connection() as conn:
        return conn.execute(
            "SELECT COUNT(*) AS c FROM cupons_uso WHERE cupom_id=? AND user_id=?",
            (cupom_id, str(user_id)),
        ).fetchone()["c"]


def avaliar(cupom: dict, user_id: int, preco: float, cargos=()) -> str | None:
    """Motivo pelo qual o cupom não vale para esta compra, em texto para o cliente. None se vale.

    `cargos` são os IDs dos cargos do membro no servidor. Um cargo na lista de bloqueados vence a permissão.
    """
    c = _normalizar(cupom)
    uid = str(user_id)
    meus_cargos = {str(x) for x in cargos}
    if not c["ativo"]:
        return "Cupom inativo."
    if c["validade"] and c["validade"] < hoje().isoformat():
        return "Cupom expirado."
    if uid in c["usuarios_bloqueados"] or (
        c["usuarios_permitidos"] and uid not in c["usuarios_permitidos"]
    ):
        return "Este cupom não é válido para a sua conta."
    if meus_cargos & set(c["cargos_bloqueados"]) or (
        c["cargos_permitidos"] and not meus_cargos & set(c["cargos_permitidos"])
    ):
        return "Seu cargo não tem direito a este cupom."
    if c["primeira_compra"] or c["min_compras"]:
        compras = _compras_aprovadas(user_id)
        if c["primeira_compra"] and compras > 0:
            return "Cupom válido apenas para primeira compra."
        if compras < c["min_compras"]:
            return f"Cupom exige pelo menos {c['min_compras']} compra(s) aprovada(s)."
    if c["limite_usos"] and c["usos_atuais"] >= c["limite_usos"]:
        return "Cupom esgotado."
    if (
        c["limite_por_usuario"]
        and _usos_da_pessoa(c["id"], user_id) >= c["limite_por_usuario"]
    ):
        return "Você já usou este cupom o máximo de vezes."
    if c["min_produto"] and preco < c["min_produto"]:
        return f"Cupom exige produto com valor mínimo de R$ {c['min_produto']:.2f}."
    return None


def desconto(cupom: dict, preco: float) -> float:
    """Quanto o cupom abate do preço (nunca passa do preço)."""
    c = _normalizar(cupom)
    if c["tipo_desconto"] == "valor":
        return round(min(float(c["desconto_valor"] or 0), preco), 2)
    return round(preco * int(c["desconto_pct"] or 0) / 100, 2)
