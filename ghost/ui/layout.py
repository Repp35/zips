"""Mensagens em Components V2 no lugar de embeds: cartões com texto, separadores e botões.

Tudo aqui vira payload cru para a API (ver services/rest.py). Botões saem de catalogo.render.botao.
"""

from datetime import datetime

FLAG_V2 = 1 << 15
EFEMERA = 64


def texto(markdown: str) -> dict:
    return {"type": 10, "content": markdown}


def titulo(markdown: str) -> dict:
    return texto(f"## {markdown}")


def rodape(markdown: str) -> dict:
    return texto(f"-# {markdown}")


def separador(grande: bool = False) -> dict:
    return {"type": 14, "divider": True, "spacing": 2 if grande else 1}


def imagem(url: str) -> dict:
    return {"type": 12, "items": [{"media": {"url": url}}]}


def cartao(*itens: dict, cor: int | None = None) -> dict:
    bloco = {"type": 17, "components": list(itens)}
    if cor is not None:
        bloco["accent_color"] = cor
    return bloco


def mensagem(*cartoes: dict, efemera: bool = False) -> dict:
    flags = FLAG_V2 | (EFEMERA if efemera else 0)
    return {"flags": flags, "components": list(cartoes)}


def pares(itens: list[tuple[str, str]]) -> str:
    """Campos como linhas de texto: **Nome:** valor."""
    return "\n".join(f"**{nome}:** {valor}" for nome, valor in itens)


def carimbo() -> str:
    return f"{datetime.now():%d/%m/%Y %H:%M}"


def log(titulo_: str, cor: int, campos: list[tuple[str, str]]) -> dict:
    corpo = [titulo(titulo_)]
    if campos:
        corpo.append(texto(pares(campos)))
    corpo.append(separador())
    corpo.append(rodape(carimbo()))
    return mensagem(cartao(*corpo, cor=cor))


def linha(*componentes: dict) -> dict:
    """Linha de botões ou seletor. Uma linha dentro de outra (ex.: voltar() dentro de linha()) é achatada:
    o Discord não aceita Action Row dentro de Action Row."""
    achatados: list[dict] = []
    for c in componentes:
        if isinstance(c, dict) and c.get("type") == 1:
            achatados.extend(c.get("components", []))
        else:
            achatados.append(c)
    return {"type": 1, "components": achatados}


def seletor(custom_id: str, placeholder: str, opcoes: list[dict]) -> dict:
    return {
        "type": 3,
        "custom_id": custom_id,
        "placeholder": placeholder,
        "options": opcoes,
    }
