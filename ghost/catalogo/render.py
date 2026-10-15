"""Converte os dados do catálogo em componentes Components V2 e resolve placeholders e emojis.

Placeholders: {chave} vem do contexto da vitrine ou do carrinho; {E.nome} vira o emoji do servidor.
Em campos de emoji (botões e opções): "@nome" usa um asset, "<:nome:id>" usa um emoji de marca.
"""

import re


from ..ui.emojis import E, EMOJIS

_EMOJI_NO_TEXTO = re.compile(r"\{E\.(\w+)\}")
_PLACEHOLDER = re.compile(r"\{(\w+)\}")
_MARKUP = re.compile(r"<a?:(\w+):(\d+)>")


def brl(valor: float) -> str:
    return f"R$ {valor:.2f}".replace(".", ",")


def texto(valor: str, ctx: dict | None = None) -> str:
    ctx = ctx or {}
    valor = _EMOJI_NO_TEXTO.sub(
        lambda m: str(getattr(E, m.group(1))) if m.group(1) in EMOJIS else m.group(0),
        valor,
    )
    return _PLACEHOLDER.sub(
        lambda m: str(ctx[m.group(1)]) if m.group(1) in ctx else m.group(0), valor
    )


def emoji(valor: str | None) -> dict | None:
    if not valor:
        return None
    if valor.startswith("@"):
        return getattr(E, valor[1:]).payload()
    marca = _MARKUP.fullmatch(valor)
    if marca:
        return {"name": marca.group(1), "id": marca.group(2)}
    return {"name": valor}


def botao(
    label: str, custom_id: str, style: int = 2, emoji_valor: str | None = None
) -> dict:
    payload = {"type": 2, "style": style, "label": label, "custom_id": custom_id}
    icone = emoji(emoji_valor)
    if icone:
        payload["emoji"] = icone
    return payload


def blocos(lista: list[dict], ctx: dict) -> list[dict]:
    """Blocos de texto, linha, imagem e estoque, na ordem em que aparecem no JSON."""
    saida = []
    for bloco in lista:
        tipo = bloco["tipo"]
        if tipo == "texto":
            saida.append({"type": 10, "content": texto(bloco["texto"], ctx)})
        elif tipo == "linha":
            saida.append({"type": 14, "divider": True, "spacing": 1})
        elif tipo == "imagem":
            url = texto(bloco["url"], ctx) if bloco.get("url") else ctx.get("imagem")
            if url:
                saida.append({"type": 12, "items": [{"media": {"url": url}}]})
        elif tipo == "estoque":
            saida.append({"type": 10, "content": ctx["estoque"]})
        else:
            raise ValueError(f"bloco desconhecido: {tipo}")
    return saida
