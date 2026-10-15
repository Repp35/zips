"""Catálogo da loja. A fonte da verdade é um arquivo JSON por produto, em produtos/<id>.json.

O arquivo tem tudo sobre o produto: preço, ordem, canal, modelo de entrega, mensagem do carrinho, a opção do
menu (select) e o bloco da vitrine onde ele aparece. A vitrine é montada a partir do produto ativo de menor ordem
dentro dela; os outros produtos da vitrine carregam uma cópia do mesmo bloco. Vitrines sem produto (SUPORTE,
PEDIDO_RESTOCK) ficam em VITRINES_DO_SISTEMA.

Erro de estrutura em qualquer arquivo impede o boot e mostra o nome do arquivo. Quem grava produto usa
services/produtos.py, que confere tudo com normalizar() antes de gravar.
"""

import copy
import json
import logging
import os
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from ..config import settings

log = logging.getLogger("catalogo")

PASTA = Path(__file__).resolve().parent
_produtos_configurado = os.getenv("PRODUTOS_DIR", "").strip()
if _produtos_configurado:
    _produtos_path = Path(_produtos_configurado).expanduser()
    PASTA_PRODUTOS = (
        _produtos_path
        if _produtos_path.is_absolute()
        else settings.BASE_DIR / _produtos_path
    )
else:
    PASTA_PRODUTOS = PASTA / "produtos"
ENTREGAS = ("automatica", "manual")
ESTOQUES = (None, "conta", "link")
SEM_ESTOQUE = ("avisar", "manual")
TIPOS_VITRINE = ("selecao", "selecao_simples", "painel")
TIPOS_CARRINHO = ("texto", "linha", "imagem", "botoes")
TIPOS_BLOCO_VITRINE = ("texto", "linha", "imagem", "estoque")
ACOES_BOTAO = ("pagar", "lote", "cupom", "cancelar")
CAMPOS_SELETOR = ("custom_id", "placeholder", "imagem", "titulo", "descricao", "cor")
CAMPOS_MODELO = ("estoque", "entrega", "sem_estoque", "pagamento", "pagamento_manual")
CAMPOS_PRODUTO = (
    "id",
    "nome",
    "preco",
    "ordem",
    "ativo",
    "anunciar",
    "canal",
    "imagem",
    "garantia",
    "select",
    "botao",
    "sem_estoque",
    "modelo",
    "mensagem",
    "vitrine",
)
CAMPOS_SELECT = ("rotulo", "descricao", "emoji")
CAMPOS_BOTAO = ("label", "style", "emoji")
DESCRICAO_OPCAO = "{preco} | {estado}"
ID_PRODUTO = re.compile(r"[a-z0-9_]{2,40}")
ID_VITRINE = re.compile(r"[A-Za-z0-9_]{2,30}")


class CatalogoInvalido(Exception):
    pass


@dataclass(frozen=True)
class Modelo:
    nome: str  # id do produto dono: o modelo é do produto
    estoque: str | None
    entrega: str
    sem_estoque: str
    pagamento: str
    pagamento_manual: str
    carrinho: dict  # a mensagem do produto: {"automatico": [...], "manual": [...]}


@dataclass(frozen=True)
class Produto:
    id: str
    nome: str
    rotulo: str  # nome da opção no select (select.rotulo)
    preco: float
    vitrine: str  # id da vitrine onde aparece
    ordem: int
    modelo: Modelo
    sem_estoque: str
    ativo: bool
    anunciar: bool
    imagem: str | None
    emoji: str | None  # emoji da opção no select (select.emoji)
    garantia: str | None
    botao: dict
    canal: int  # canal da vitrine, dado pelo produto que a abre; 0 = usa o canal configurado
    descricao: str  # texto da opção no select, com {preco} e {estado}


@dataclass(frozen=True)
class Vitrine:
    id: str
    tipo: str
    nome_setup: str
    publicar: bool
    dados: dict


# ── Modelos padrão: só para começar um produto novo. Depois de criado, o produto guarda a própria cópia ─────────

_PAGAMENTO_AUTO = "Pagamento via PIX — entrega automática após confirmação."
_PAGAMENTO_MANUAL = (
    "Pagamento via PIX — entrega **manual** realizada por um membro da equipe."
)

_MENSAGEM_MANUAL = [
    {"tipo": "texto", "texto": "## {titulo}\n\n**Valor:** {preco}\n\n{pagamento}"},
    {"tipo": "linha"},
    {"tipo": "imagem"},
    {"tipo": "linha"},
    {
        "tipo": "botoes",
        "itens": [
            {"acao": "pagar", "label": "Pagar agora", "style": 3, "emoji": "@correct"},
            {"acao": "cancelar", "label": "Cancelar", "style": 4, "emoji": "@delete"},
        ],
    },
]

_MENSAGEM_ESTOQUE = [
    {
        "tipo": "texto",
        "texto": "## {titulo}\n\n{valor}{cupom}\n**Em estoque:** {estoque}\n\n{pagamento}{garantia}",
    },
    {"tipo": "linha"},
    {"tipo": "imagem"},
    {"tipo": "linha"},
    {
        "tipo": "botoes",
        "itens": [
            {"acao": "pagar", "label": "Pagar agora", "style": 3, "emoji": "@correct"},
            {
                "acao": "lote",
                "label": "Lote ({quantidade}x)",
                "style": 2,
                "emoji": "@cardbox",
            },
            {
                "acao": "cupom",
                "label": "Cupom",
                "label_com_cupom": "Cupom: {cupom_codigo}",
                "style": 2,
                "emoji": "@ticket",
            },
            {"acao": "cancelar", "label": "Cancelar", "style": 4, "emoji": "@delete"},
        ],
    },
]

PADROES: dict[str, dict] = {
    "conta": {
        "modelo": {
            "estoque": "conta",
            "entrega": "automatica",
            "sem_estoque": "avisar",
            "pagamento": _PAGAMENTO_AUTO,
            "pagamento_manual": _PAGAMENTO_MANUAL,
        },
        "mensagem": {"automatico": _MENSAGEM_ESTOQUE, "manual": _MENSAGEM_MANUAL},
    },
    "link": {
        "modelo": {
            "estoque": "link",
            "entrega": "automatica",
            "sem_estoque": "avisar",
            "pagamento": _PAGAMENTO_AUTO,
            "pagamento_manual": _PAGAMENTO_MANUAL,
        },
        "mensagem": {"automatico": _MENSAGEM_ESTOQUE, "manual": _MENSAGEM_MANUAL},
    },
    "manual": {
        "modelo": {
            "estoque": None,
            "entrega": "manual",
            "sem_estoque": "manual",
            "pagamento": _PAGAMENTO_MANUAL,
            "pagamento_manual": _PAGAMENTO_MANUAL,
        },
        "mensagem": {"automatico": _MENSAGEM_MANUAL, "manual": _MENSAGEM_MANUAL},
    },
}


def padrao(tipo: str) -> dict:
    """Modelo e mensagem de um tipo padrão (conta, link ou manual). Cópia: quem usa pode alterar à vontade."""
    return copy.deepcopy(PADROES[tipo])


# ── Vitrines sem produto: não podem ser montadas a partir de um produto, então ficam aqui ─────────────────────────

VITRINES_DO_SISTEMA = {
    "PEDIDO_RESTOCK": {
        "id": "PEDIDO_RESTOCK",
        "tipo": "painel",
        "nome_setup": "Pedido Restock",
        "publicar": True,
        "blocos": [
            {
                "tipo": "texto",
                "texto": "## {E.cardbox}  Solicitar Restock\n\nSentiu falta de algum produto? Selecione abaixo e a equipe será notificada imediatamente.\nQuando o estoque for reabastecido, você poderá optar por receber um aviso.",
            },
            {
                "tipo": "linha",
            },
            {
                "tipo": "texto",
                "texto": "-# Apenas produtos atualmente **sem estoque** aparecem no menu.\n-# Seu pedido é privado — a equipe cuidará do resto.",
            },
        ],
        "acoes": [
            {
                "label": "Solicitar Restock",
                "style": 2,
                "custom_id": "pedido_restock_btn",
            },
        ],
    },
    "SUPORTE": {
        "id": "SUPORTE",
        "tipo": "selecao_simples",
        "nome_setup": "Suporte",
        "publicar": True,
        "titulo": "Central de Atendimento | Ghost Store",
        "descricao": "Ao abrir um ticket, um membro da nossa equipe entrará em contato o mais rápido possível.\nTodo atendimento é realizado de forma privada e segura.\nRespondemos todos os tickets.\nCaso não receba retorno imediato, não se preocupe – sua mensagem foi recebida e será respondida em breve.",
        "cor": 1118484,
        "imagem": "https://cdn.discordapp.com/attachments/1498777322319384847/1499289397714096158/19_Sem_Titulo_20260429221552.png?ex=69f4ea4f&is=69f398cf&hm=4e4a2868275b718e6c5f42d93defee821a80b34b0cec347acf49ac478f03c7c6&",
        "custom_id": "suporte_tipo_select",
        "placeholder": "Clique aqui para ver as opções",
        "opcoes": [
            {
                "label": "Suporte",
                "value": "suporte",
                "description": "Preciso de ajuda",
                "emoji": "<:duvida:1499220609350107167>",
            },
            {
                "label": "Receber Produto",
                "value": "produto",
                "description": "Quero receber meu produto",
                "emoji": "@cardbox",
            },
        ],
    },
}

_SISTEMA: dict[str, dict] = {}


# ── Conferência ───────────────────────────────────────────────────────────────


def _exigir(condicao: bool, origem: str, mensagem: str) -> None:
    if not condicao:
        raise CatalogoInvalido(f"{origem}: {mensagem}")


def _ler(caminho: Path) -> dict:
    try:
        dados = json.loads(caminho.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CatalogoInvalido(f"{caminho.name}: {exc}") from exc
    _exigir(
        isinstance(dados, dict), caminho.name, "o arquivo precisa ser um objeto JSON"
    )
    return dados


def _bool(valor, origem: str, campo: str) -> bool:
    _exigir(isinstance(valor, bool), origem, f"{campo} precisa ser true ou false")
    return valor


def _validar_carrinho(blocos, origem: str, onde: str) -> None:
    _exigir(isinstance(blocos, list), origem, f"{onde} precisa ser uma lista de blocos")
    for bloco in blocos:
        tipo = bloco.get("tipo") if isinstance(bloco, dict) else None
        _exigir(tipo in TIPOS_CARRINHO, origem, f"{onde}: bloco inválido {tipo!r}")
        if tipo == "texto":
            _exigir(
                isinstance(bloco.get("texto"), str),
                origem,
                f"{onde}: bloco de texto precisa de 'texto'",
            )
        if tipo == "botoes":
            itens = bloco.get("itens")
            _exigir(
                isinstance(itens, list) and 1 <= len(itens) <= 5,
                origem,
                f"{onde}: botoes precisa de 1 a 5 itens",
            )
            for item in itens:
                acao = item.get("acao") if isinstance(item, dict) else None
                _exigir(
                    acao in ACOES_BOTAO,
                    origem,
                    f"{onde}: ação de botão inválida {acao!r}",
                )
                _exigir(
                    isinstance(item.get("label"), str)
                    and 1 <= len(item["label"]) <= 80,
                    origem,
                    f"{onde}: label obrigatório, até 80 caracteres",
                )
                _exigir(
                    item.get("style", 2) in (1, 2, 3, 4),
                    origem,
                    f"{onde}: style inválido",
                )


def _modelo_de(d, origem: str) -> dict:
    _exigir(isinstance(d, dict), origem, "modelo precisa ser um objeto")
    extras = sorted(set(d) - set(CAMPOS_MODELO))
    _exigir(not extras, origem, f"modelo: campos desconhecidos {', '.join(extras)}")
    _exigir(
        d.get("entrega") in ENTREGAS,
        origem,
        "modelo.entrega inválida (automatica ou manual)",
    )
    _exigir(
        d.get("estoque") in ESTOQUES,
        origem,
        "modelo.estoque inválido (null, conta ou link)",
    )
    _exigir(
        d.get("sem_estoque", "avisar") in SEM_ESTOQUE,
        origem,
        "modelo.sem_estoque inválido (avisar ou manual)",
    )
    for campo in ("pagamento", "pagamento_manual"):
        _exigir(
            isinstance(d.get(campo, ""), str),
            origem,
            f"modelo.{campo} precisa ser texto",
        )
    return {
        "estoque": d.get("estoque"),
        "entrega": d["entrega"],
        "sem_estoque": d.get("sem_estoque", "avisar"),
        "pagamento": d.get("pagamento", ""),
        "pagamento_manual": d.get("pagamento_manual", ""),
    }


def _mensagem_de(d, origem: str) -> dict:
    _exigir(
        isinstance(d, dict) and set(d) == {"automatico", "manual"},
        origem,
        "mensagem precisa ter 'automatico' e 'manual' (e só eles)",
    )
    for variante in ("automatico", "manual"):
        _validar_carrinho(d[variante], origem, f"mensagem.{variante}")
    return {
        "automatico": copy.deepcopy(d["automatico"]),
        "manual": copy.deepcopy(d["manual"]),
    }


def _select_de(d, nome: str, origem: str) -> dict:
    d = {} if d is None else d
    _exigir(isinstance(d, dict), origem, "select precisa ser um objeto")
    extras = sorted(set(d) - set(CAMPOS_SELECT))
    _exigir(not extras, origem, f"select: campos desconhecidos {', '.join(extras)}")
    rotulo = d.get("rotulo") or nome
    descricao = d.get("descricao") or DESCRICAO_OPCAO
    emoji = d.get("emoji") or None
    _exigir(
        isinstance(rotulo, str) and len(rotulo) <= 100,
        origem,
        "select.rotulo: texto de até 100 caracteres",
    )
    _exigir(
        isinstance(descricao, str) and len(descricao) <= 100,
        origem,
        "select.descricao: texto de até 100 caracteres",
    )
    _exigir(
        emoji is None or isinstance(emoji, str),
        origem,
        "select.emoji precisa ser texto",
    )
    return {"rotulo": rotulo, "descricao": descricao, "emoji": emoji}


def _botao_de(d, origem: str) -> dict:
    _exigir(isinstance(d, dict), origem, "botao precisa ser um objeto")
    extras = sorted(set(d) - set(CAMPOS_BOTAO))
    _exigir(not extras, origem, f"botao: campos desconhecidos {', '.join(extras)}")
    if not d:
        return {}
    label = d.get("label", "Comprar")
    style = d.get("style", 2)
    _exigir(
        isinstance(label, str) and 0 < len(label) <= 80,
        origem,
        "botao.label: texto de 1 a 80 caracteres",
    )
    _exigir(
        isinstance(style, int)
        and not isinstance(style, bool)
        and style in (1, 2, 3, 4),
        origem,
        "botao.style: 1, 2, 3 ou 4",
    )
    emoji = d.get("emoji") or None
    _exigir(
        emoji is None or isinstance(emoji, str), origem, "botao.emoji precisa ser texto"
    )
    return {"label": label, "style": style, "emoji": emoji}


def bloco_vitrine(d, origem: str) -> dict:
    """Confere o bloco de vitrine (o que aparece no Discord) e devolve a versão completa."""
    _exigir(
        isinstance(d, dict),
        origem,
        "vitrine precisa ser um objeto (ou o ID de uma vitrine existente)",
    )
    vid = d.get("id")
    _exigir(
        isinstance(vid, str) and ID_VITRINE.fullmatch(vid) is not None,
        origem,
        "vitrine.id: de 2 a 30 letras, números ou _",
    )
    tipo = d.get("tipo")
    _exigir(tipo in TIPOS_VITRINE, origem, f"vitrine.tipo inválido: {tipo!r}")
    if tipo == "painel":
        permitidos = {
            "id",
            "tipo",
            "nome_setup",
            "publicar",
            "blocos",
            "seletor",
            "acoes",
        }
    else:
        permitidos = {"id", "tipo", "nome_setup", "publicar", *CAMPOS_SELETOR}
        if tipo == "selecao_simples":
            permitidos.add("opcoes")
    extras = sorted(set(d) - permitidos)
    _exigir(
        not extras, origem, f"vitrine {tipo}: campos desconhecidos {', '.join(extras)}"
    )
    bloco = {
        "id": vid,
        "tipo": tipo,
        "nome_setup": d.get("nome_setup") or vid,
        "publicar": _bool(d.get("publicar", True), origem, "vitrine.publicar"),
    }
    for chave, valor in d.items():
        if chave not in bloco:
            bloco[chave] = copy.deepcopy(valor)
    if tipo == "painel":
        _exigir(
            isinstance(d.get("blocos"), list),
            origem,
            "vitrine painel precisa de 'blocos' (uma lista)",
        )
        for item in d["blocos"]:
            btipo = item.get("tipo") if isinstance(item, dict) else None
            _exigir(
                btipo in TIPOS_BLOCO_VITRINE,
                origem,
                f"bloco de vitrine inválido: {btipo!r}",
            )
            if btipo == "texto":
                _exigir(
                    isinstance(item.get("texto"), str),
                    origem,
                    "bloco de texto da vitrine precisa de 'texto'",
                )
        if "seletor" in d:
            _exigir(
                isinstance(d["seletor"], dict)
                and {"custom_id", "placeholder"} <= d["seletor"].keys(),
                origem,
                "seletor precisa de 'custom_id' e 'placeholder'",
            )
        if "acoes" in d:
            _exigir(
                isinstance(d["acoes"], list),
                origem,
                "acoes precisa ser uma lista de botões",
            )
    else:
        for campo in CAMPOS_SELETOR:
            _exigir(campo in d, origem, f"vitrine {tipo}: falta o campo '{campo}'")
        if tipo == "selecao_simples":
            opcoes = d.get("opcoes")
            _exigir(
                isinstance(opcoes, list) and opcoes,
                origem,
                "selecao_simples precisa de 'opcoes'",
            )
            for opcao in opcoes:
                _exigir(
                    isinstance(opcao, dict)
                    and {"label", "value", "description"} <= opcao.keys(),
                    origem,
                    "cada opção precisa de 'label', 'value' e 'description'",
                )
    return bloco


def _canal_de(valor, origem: str) -> str | None:
    if valor in (None, "", 0, "0") and not isinstance(valor, bool):
        return None
    _exigir(
        not isinstance(valor, bool) and str(valor).isdigit(),
        origem,
        "canal precisa ser o ID numérico do canal (ou vazio)",
    )
    return str(valor)


def chave_cabeca(doc: dict) -> tuple:
    """Quem abre a vitrine: o produto ativo de menor ordem. Sem ativo, o de menor ordem (a vitrine não some)."""
    return (not doc["ativo"], doc["ordem"], doc["id"])


def normalizar(dados: dict, origem: str = "produto") -> dict:
    """Confere o documento completo do produto e devolve a versão final, na ordem do arquivo.
    Levanta CatalogoInvalido com a mensagem do problema. Não aceita atalhos: modelo e vitrine são objetos."""
    _exigir(isinstance(dados, dict), origem, "o produto precisa ser um objeto JSON")
    extras = sorted(set(dados) - set(CAMPOS_PRODUTO))
    _exigir(not extras, origem, f"campos desconhecidos: {', '.join(extras)}")
    pid = dados.get("id")
    _exigir(
        isinstance(pid, str) and ID_PRODUTO.fullmatch(pid) is not None,
        origem,
        "id: de 2 a 40 letras minúsculas, números ou _",
    )
    nome = dados.get("nome")
    _exigir(isinstance(nome, str) and nome.strip() != "", origem, "nome obrigatório")
    preco = dados.get("preco")
    _exigir(
        isinstance(preco, (int, float))
        and not isinstance(preco, bool)
        and 0 <= preco < 1_000_000,
        origem,
        "preço inválido",
    )
    ordem = dados.get("ordem", 0)
    _exigir(
        isinstance(ordem, int) and not isinstance(ordem, bool) and ordem >= 0,
        origem,
        "ordem precisa ser um número inteiro, 0 ou maior",
    )
    imagem = dados.get("imagem") or None
    _exigir(
        imagem is None or (isinstance(imagem, str) and imagem.startswith("https://")),
        origem,
        "imagem precisa ser um link https://",
    )
    garantia = dados.get("garantia") or None
    _exigir(
        garantia is None or isinstance(garantia, str),
        origem,
        "garantia precisa ser texto",
    )
    sem_estoque = dados.get("sem_estoque") or None
    _exigir(
        sem_estoque is None or sem_estoque in SEM_ESTOQUE,
        origem,
        "sem_estoque inválido (avisar ou manual)",
    )
    botao = _botao_de(dados.get("botao") or {}, origem)

    doc = {
        "id": pid,
        "nome": nome,
        "preco": round(float(preco), 2),
        "ordem": ordem,
        "ativo": _bool(dados.get("ativo", True), origem, "ativo"),
        "anunciar": _bool(dados.get("anunciar", True), origem, "anunciar"),
        "canal": _canal_de(dados.get("canal"), origem),
        "imagem": imagem,
        "garantia": garantia,
        "select": _select_de(dados.get("select"), nome, origem),
    }
    if botao:
        doc["botao"] = botao
    if sem_estoque:
        doc["sem_estoque"] = sem_estoque
    doc["modelo"] = _modelo_de(dados.get("modelo"), origem)
    doc["mensagem"] = _mensagem_de(dados.get("mensagem"), origem)
    doc["vitrine"] = bloco_vitrine(dados.get("vitrine"), origem)
    return doc


def validar(dados: dict) -> Produto:
    """Confere um produto completo sem gravar nada e devolve o objeto."""
    return _produto(normalizar(dados))


def _produto(doc: dict) -> Produto:
    modelo = Modelo(
        nome=doc["id"],
        estoque=doc["modelo"]["estoque"],
        entrega=doc["modelo"]["entrega"],
        sem_estoque=doc["modelo"]["sem_estoque"],
        pagamento=doc["modelo"]["pagamento"],
        pagamento_manual=doc["modelo"]["pagamento_manual"],
        carrinho=doc["mensagem"],
    )
    return Produto(
        id=doc["id"],
        nome=doc["nome"],
        rotulo=doc["select"]["rotulo"],
        preco=doc["preco"],
        vitrine=doc["vitrine"]["id"],
        ordem=doc["ordem"],
        modelo=modelo,
        sem_estoque=doc.get("sem_estoque") or modelo.sem_estoque,
        ativo=doc["ativo"],
        anunciar=doc["anunciar"],
        imagem=doc["imagem"],
        emoji=doc["select"]["emoji"],
        garantia=doc["garantia"],
        botao=doc.get("botao", {}),
        canal=int(doc["canal"] or 0),
        descricao=doc["select"]["descricao"],
    )


def _vitrine_de(bloco: dict) -> Vitrine:
    return Vitrine(
        id=bloco["id"],
        tipo=bloco["tipo"],
        nome_setup=bloco["nome_setup"],
        publicar=bloco["publicar"],
        dados=copy.deepcopy(bloco),
    )


# ── Carregamento ──────────────────────────────────────────────────────────────

PRODUTOS: dict[str, Produto] = {}
VITRINES: dict[str, Vitrine] = {}
_DOCS: dict[str, dict] = {}  # documento completo de cada produto, como está no arquivo


def _montar_vitrines(docs: dict[str, dict]) -> dict[str, Vitrine]:
    grupos: dict[str, list[dict]] = {}
    for doc in docs.values():
        grupos.setdefault(doc["vitrine"]["id"], []).append(doc)
    vitrines = {}
    for vid, membros in grupos.items():
        if vid in VITRINES_DO_SISTEMA:
            raise CatalogoInvalido(
                f"{membros[0]['id']}.json: a vitrine {vid} é do sistema e não aceita produtos"
            )
        cabeca = min(membros, key=chave_cabeca)
        bloco = cabeca["vitrine"]
        for doc in membros:
            if doc is not cabeca and doc["vitrine"] != bloco:
                log.warning(
                    "%s.json: bloco da vitrine %s difere de %s, que abre a vitrine; vale o de %s",
                    doc["id"],
                    vid,
                    cabeca["id"],
                    cabeca["id"],
                )
        if bloco["tipo"] == "painel" and len(membros) > 1 and "seletor" not in bloco:
            raise CatalogoInvalido(
                f"{cabeca['id']}.json: a vitrine {vid} tem mais de um produto e precisa de 'seletor'"
            )
        vitrines[vid] = _vitrine_de(bloco)
    for vid, bloco in _SISTEMA.items():
        vitrines[vid] = _vitrine_de(bloco)
    return vitrines


def recarregar() -> None:
    """Lê todos os arquivos de produto e monta produtos e vitrines. Levanta CatalogoInvalido com o arquivo culpado."""
    global PRODUTOS, VITRINES, _DOCS
    docs: dict[str, dict] = {}
    for caminho in sorted(PASTA_PRODUTOS.glob("*.json")):
        bruto = _ler(caminho)
        _exigir(
            bruto.get("id") == caminho.stem,
            caminho.name,
            "id deve ser igual ao nome do arquivo",
        )
        docs[caminho.stem] = normalizar(bruto, caminho.name)
    produtos = {pid: _produto(doc) for pid, doc in docs.items()}
    vitrines = _montar_vitrines(docs)
    PRODUTOS, VITRINES, _DOCS = produtos, vitrines, docs


_SISTEMA.update(
    {
        vid: bloco_vitrine(d, f"vitrine do sistema {vid}")
        for vid, d in VITRINES_DO_SISTEMA.items()
    }
)


# ── Consultas ─────────────────────────────────────────────────────────────────


def _copia(d: dict | None) -> dict | None:
    return copy.deepcopy(d) if d is not None else None


def existe(produto_id: str) -> bool:
    return produto_id in _DOCS


def gerar_id(nome: str) -> str:
    """ID a partir do nome: 'Netflix Premium 4K' vira netflix_premium_4k. Se já existir, ganha _2, _3..."""
    base = (
        unicodedata.normalize("NFKD", nome).encode("ascii", "ignore").decode().lower()
    )
    base = re.sub(r"[^a-z0-9]+", "_", base).strip("_")[:36] or "produto"
    if len(base) < 2:
        base += "_produto"
    pid, n = base, 2
    while existe(pid):
        pid = f"{base}_{n}"
        n += 1
    return pid


def produto(produto_id: str) -> Produto | None:
    return PRODUTOS.get(produto_id)


def produtos(vitrine: str | None = None, *, so_ativos: bool = True) -> list[Produto]:
    lista = [
        p
        for p in PRODUTOS.values()
        if (vitrine is None or p.vitrine == vitrine) and (p.ativo or not so_ativos)
    ]
    return sorted(lista, key=lambda p: (p.vitrine, p.ordem, p.id))


def vitrine(vitrine_id: str) -> Vitrine | None:
    return VITRINES.get(vitrine_id)


def vitrines() -> list[Vitrine]:
    return list(VITRINES.values())


def doc_produto(produto_id: str) -> dict | None:
    """Documento completo do produto, como está no arquivo. None se não existe."""
    return _copia(_DOCS.get(produto_id))


def dados_brutos(produto_id: str) -> dict:
    """Documento completo do produto, ou só o ID se ele ainda não existe."""
    return doc_produto(produto_id) or {"id": produto_id}


def documentos_da_vitrine(vitrine_id: str) -> list[dict]:
    """Documentos completos dos produtos de uma vitrine (ativos ou não). Cópias."""
    return [
        copy.deepcopy(d) for d in _DOCS.values() if d["vitrine"]["id"] == vitrine_id
    ]


def doc_vitrine(vitrine_id: str) -> dict | None:
    """Bloco de vitrine em uso (o do produto que a abre, ou o do sistema). Cópia."""
    v = VITRINES.get(vitrine_id)
    return copy.deepcopy(v.dados) if v is not None else None


def canal_da_vitrine(vitrine_id: str) -> int:
    """Canal definido pelo produto que abre a vitrine. 0 quando ele não define um."""
    membros = documentos_da_vitrine(vitrine_id)
    if not membros:
        return 0
    return int(min(membros, key=chave_cabeca)["canal"] or 0)


# ── Gravação (só pelo services/produtos.py) ───────────────────────────────────


def gravar(doc: dict) -> None:
    """Grava o arquivo do produto. O documento é conferido antes e o arquivo é trocado por inteiro.
    Quem chama deve recarregar o catálogo depois."""
    limpo = normalizar(doc, str(doc.get("id", "produto")))
    destino = PASTA_PRODUTOS / f"{limpo['id']}.json"
    from ..core.files import atomic_text

    atomic_text(destino, json.dumps(limpo, ensure_ascii=False, indent=2) + "\n")


def remover(produto_id: str) -> None:
    """Apaga o arquivo do produto. Quem chama deve recarregar o catálogo depois."""
    if not ID_PRODUTO.fullmatch(produto_id):
        raise CatalogoInvalido("ID inválido")
    (PASTA_PRODUTOS / f"{produto_id}.json").unlink(missing_ok=True)


recarregar()
