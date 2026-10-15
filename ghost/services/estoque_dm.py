"""Estoque por JSON. Cada JSON é um item, e o item tem os campos que vão aparecer na entrega.
Os nomes dos campos são livres. Dois nomes são especiais: "Adicional" e "Imagem".

    {"Email": "conta@x.com", "Senha": "123", "Pin": "4321",
     "Imagem": "https://exemplo.com/banner.png", "Adicional": "Troque a senha no primeiro acesso."}

- Modelo por produto: escolhido no painel entre os MODELOS (ex.: email e senha, link). Define o JSON padrão.
- Entrega: Produto e Valor pago primeiro, depois os campos do item, cada um num campo do embed.
- "Imagem": link https://. Aparece grande, embaixo da entrega. "Miniatura": link https://, pequena, dentro da entrega.
- "Adicional": vai por último, com uma linha em branco antes. Vazio = sem mensagem adicional.
- "Adicional": "[entrega ticket]" = entrega manual: os dados não aparecem na mensagem.
- Sem estoque com entrega manual: mesmo embed, sem campos de item, com o campo Aviso.
- Título, rodapé, produto, data e preço são fixos.
- Produto de link: o primeiro campo (sem contar Adicional e Imagem) precisa ser o link https://...
"""

import json
from datetime import datetime

from ..data import store
from ..ui.emojis import E

TITULO = "⚡ Entrega Realizada"
MANUAL = "Abra um ticket no suporte para receber o produto."
ITENS_MAX = 500
CAMPOS_MAX = 20
LIMITE_NOME = 100
LIMITE_VALOR = 500
LIMITE_ADICIONAL = 1000
LIMITE_IMAGEM = 512
ADICIONAL = "adicional"
# Nomes de imagem e onde cada um aparece. Só uma imagem por item.
POSICOES_IMAGEM = {"imagem": "baixo", "miniatura": "dentro"}
ESPECIAIS = (ADICIONAL, *POSICOES_IMAGEM)  # nomes que não viram campo da entrega
TICKET = "[entrega ticket]"
RESERVADOS = ("produto", "valor pago")

# Modelos predefinidos. Cada produto escolhe um: o JSON padrão dos itens dele (sem o produto em si).
MODELOS = {
    "email_senha": {
        "titulo": "Email e senha",
        "tipo": "conta",
        "campos": ["Email", "Senha", "Imagem", "Adicional"],
    },
    "email_senha_pin": {
        "titulo": "Email, senha e pin",
        "tipo": "conta",
        "campos": ["Email", "Senha", "Pin", "Imagem", "Adicional"],
    },
    "link": {
        "titulo": "Link",
        "tipo": "link",
        "campos": ["Link", "Imagem", "Adicional"],
    },
    "link_pin": {
        "titulo": "Link e pin",
        "tipo": "link",
        "campos": ["Link", "Pin", "Imagem", "Adicional"],
    },
}
EXEMPLOS_CAMPO = {
    "Email": "conta@x.com",
    "Senha": "senha123",
    "Pin": "4321",
    "Link": "https://resgate.com/xyz",
    "Imagem": "https://exemplo.com/banner.png",
    "Adicional": "Troque a senha no primeiro acesso.",
}


class EstoqueInvalido(ValueError):
    """Mensagem para quem preencheu o JSON."""


def _txt(valor, campo: str, limite: int) -> str:
    if not isinstance(valor, str):
        raise EstoqueInvalido(f"'{campo}' precisa ser texto")
    valor = valor.strip()
    if not valor:
        raise EstoqueInvalido(f"'{campo}' não pode ficar vazio")
    if len(valor) > limite:
        raise EstoqueInvalido(f"'{campo}' passa de {limite} caracteres")
    return valor


def _campos(bruto, n: int) -> dict:
    """Um item: objeto com campos. Adicional e Imagem vazios são descartados."""
    if not isinstance(bruto, dict) or not bruto:
        raise EstoqueInvalido(
            f'item {n} precisa ser um objeto com campos, como {{"Email": "..."}}'
        )
    if len(bruto) > CAMPOS_MAX:
        raise EstoqueInvalido(f"item {n}: no máximo {CAMPOS_MAX} campos")
    campos = {}
    for nome, valor in bruto.items():
        rotulo = _txt(str(nome), f"item {n} nome do campo", LIMITE_NOME)
        chave = rotulo.lower()
        if chave in RESERVADOS:
            raise EstoqueInvalido(
                f"item {n}: '{rotulo}' já aparece na mensagem, tire esse campo"
            )
        if chave == ADICIONAL:
            if not isinstance(valor, str):
                raise EstoqueInvalido(f"item {n}: 'Adicional' precisa ser texto")
            valor = valor.strip()
            if not valor:
                continue
            if len(valor) > LIMITE_ADICIONAL:
                raise EstoqueInvalido(
                    f"item {n}: 'Adicional' passa de {LIMITE_ADICIONAL} caracteres"
                )
            campos[rotulo] = valor
        elif chave in POSICOES_IMAGEM:
            if not isinstance(valor, str):
                raise EstoqueInvalido(
                    f"item {n}: '{rotulo}' precisa ser um link https://"
                )
            valor = valor.strip()
            if not valor:
                continue
            if (
                not valor.startswith("https://")
                or " " in valor
                or len(valor) > LIMITE_IMAGEM
            ):
                raise EstoqueInvalido(
                    f"item {n}: '{rotulo}' precisa ser um link https:// sem espaços"
                )
            campos[rotulo] = valor
        else:
            campos[rotulo] = _txt(valor, f"item {n} {rotulo}", LIMITE_VALOR)
    if sum(1 for nome in campos if nome.lower() in POSICOES_IMAGEM) > 1:
        raise EstoqueInvalido(f"item {n}: use só uma imagem (Imagem ou Miniatura)")
    if not any(nome.lower() not in ESPECIAIS for nome in campos):
        raise EstoqueInvalido(
            f"item {n} precisa de pelo menos um campo além de Adicional e imagem"
        )
    return campos


def _primeiro(campos: dict) -> str:
    """Primeiro campo que não é especial: é ele que vira o link de um produto de link."""
    return next(v for k, v in campos.items() if k.lower() not in ESPECIAIS)


def _conferir_link(campos: dict, n: int) -> None:
    primeiro = _primeiro(campos)
    if not primeiro.startswith("https://") or " " in primeiro:
        raise EstoqueInvalido(
            f"item {n}: o link precisa começar com https:// e não ter espaços"
        )


def validar(doc, tipo_produto: str) -> list[dict]:
    """Confere o JSON. Aceita um item (objeto) ou uma lista de itens. Devolve os itens prontos para gravar."""
    if isinstance(doc, dict):
        brutos = [doc]
    elif isinstance(doc, list):
        brutos = doc
    else:
        raise EstoqueInvalido(
            'o JSON precisa ser um objeto com os campos do item, como {"Email": "..."}'
        )
    if len(brutos) > ITENS_MAX:
        raise EstoqueInvalido(f"no máximo {ITENS_MAX} itens por JSON")
    itens = [_campos(b, n) for n, b in enumerate(brutos, start=1)]
    if tipo_produto == "link":
        for n, campos in enumerate(itens, start=1):
            _conferir_link(campos, n)
    return itens


def padrao(tipo_produto: str) -> str:
    """Modelo usado quando o produto ainda não escolheu um."""
    return "email_senha" if tipo_produto == "conta" else "link"


def do_produto(produto_key: str, tipo_produto: str) -> str:
    chave = store.get_config(f"estoque_modelo:{produto_key}")
    if chave in MODELOS and MODELOS[chave]["tipo"] == tipo_produto:
        return chave
    return padrao(tipo_produto)


def definir_modelo(produto_key: str, chave: str, tipo_produto: str) -> None:
    if chave not in MODELOS:
        raise EstoqueInvalido("modelo desconhecido")
    if MODELOS[chave]["tipo"] != tipo_produto:
        raise EstoqueInvalido(
            f'o modelo "{MODELOS[chave]["titulo"]}" não serve para produto de {tipo_produto}'
        )
    store.set_config(f"estoque_modelo:{produto_key}", chave)


def modelo(chave: str) -> str:
    """JSON padrão do modelo: os campos, em branco."""
    return json.dumps(
        {campo: "" for campo in MODELOS[chave]["campos"]}, ensure_ascii=False, indent=2
    )


def exemplo(chave: str) -> str:
    """JSON de exemplo do modelo, com valores preenchidos."""
    return json.dumps(
        {campo: EXEMPLOS_CAMPO[campo] for campo in MODELOS[chave]["campos"]},
        ensure_ascii=False,
        indent=2,
    )


def json_do_item(row: dict, tipo_produto: str) -> dict:
    """Campos de um item disponível para editar: o mesmo formato usado para adicionar."""
    if tipo_produto == "link" and not row.get("extra"):
        return {"Link": row.get("link", "")}
    return _dados(dict(row, kind=tipo_produto))


# ── leitura do item guardado ──────────────────────────────────────────────────


def _dados(item: dict) -> dict:
    """Campos do item, na ordem em que foram escritos."""
    bruto = item.get("extra") or ""
    if bruto.startswith("{"):
        try:
            dados = json.loads(bruto)
        except ValueError:
            dados = None
        if isinstance(dados, dict):
            return dados
    if item.get("kind") == "link":
        return {"Link de resgate": item.get("link", "")}
    # itens antigos: email e senha em colunas próprias, e o extra solto
    dados = {"E-mail": item.get("email", ""), "Senha": item.get("senha", "")}
    if bruto:
        dados["Extra"] = bruto
    return dados


def _separar(item: dict) -> tuple[list[tuple[str, str]], str, str, str]:
    """(campos para mostrar, mensagem adicional, link da imagem, onde a imagem aparece).
    Adicional e as imagens nunca viram campo."""
    campos, adicional, imagem, posicao = [], "", "", ""
    for nome, valor in _dados(item).items():
        texto = str(valor).strip()
        if not texto:
            continue
        chave = str(nome).strip().lower()
        if chave == ADICIONAL:
            adicional = texto
        elif chave in POSICOES_IMAGEM:
            imagem, posicao = texto, POSICOES_IMAGEM[chave]
        else:
            campos.append((str(nome), texto))
    return campos, adicional, imagem, posicao


def resumo(item: dict) -> str:
    """Uma linha para o painel de estoque."""
    campos, adicional, imagem, _ = _separar(item)
    texto = " · ".join(f"{nome}: {valor}" for nome, valor in campos)
    if adicional.lower() == TICKET:
        texto += " · entrega manual"
    elif adicional:
        texto += f" · Adicional: {adicional}"
    if imagem:
        texto += " · com imagem"
    return texto


# ── mensagem de entrega ───────────────────────────────────────────────────────

VAZIO = "​"


def _valor(texto: str) -> str:
    """Link fica clicável; o resto vai em bloco de código, com crase trocada por aspas."""
    if texto.startswith("https://"):
        return texto
    return f"`{texto.replace('`', chr(39))}`"


def _espaco() -> dict:
    """Campo invisível: vira uma linha em branco entre os grupos do embed."""
    return {"name": VAZIO, "value": VAZIO, "inline": False}


def renderizar(
    item: dict | None, produto_nome: str, valor: float, loja: str, cor: int
) -> dict:
    """Mensagem de entrega. Cada parte vai num campo próprio do embed (copiável separado), quase tudo junto:
    Produto, Valor pago e os campos do item. A imagem, se houver: Imagem (grande, embaixo) ou Miniatura (pequena, dentro). Só o Adicional (ou o aviso
    de ticket) vem por último, com uma linha em branco. Sem item, ou com Adicional "[entrega ticket]", é o aviso
    de entrega manual."""
    campos, adicional, imagem, posicao = (
        _separar(item) if item is not None else ([], "", "", "")
    )
    manual = item is None or adicional.lower() == TICKET
    fields = [
        {"name": "Produto", "value": _valor(produto_nome), "inline": False},
        {"name": "Valor pago", "value": _valor(f"R$ {valor:.2f}"), "inline": False},
    ]
    if not manual:
        fields += [
            {"name": nome[:256], "value": _valor(texto)[:1024], "inline": False}
            for nome, texto in campos
        ]
    final = MANUAL if manual else adicional
    if final:
        nome_final = "Aviso" if manual else VAZIO
        fields += [
            _espaco(),
            {"name": nome_final, "value": final[:1024], "inline": False},
        ]
    embed = {
        "title": TITULO,
        "color": cor,
        "fields": fields,
        "footer": {"text": f"{loja} | {datetime.now():%H:%M}"},
    }
    payload = {"embeds": [embed]}
    if imagem.startswith("https://"):
        if posicao == "dentro":
            embed["thumbnail"] = {"url": imagem}
        else:
            embed["image"] = {"url": imagem}
    return payload
