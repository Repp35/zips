"""Carrinhos em thread privada: montagem da mensagem conforme o modelo do produto, cupom em memória e abertura."""

import discord

from .. import catalogo
from ..catalogo import render
from ..ui.components import link_button
from ..data import coupons, stock, store
from ..ui.emojis import E
from . import context, notify, rest, scheduler, shelves, locks

_cupons: dict[str, dict] = {}


def set_cupom(thread_id: int | str, codigo: str, cupom: dict) -> None:
    _cupons[str(thread_id)] = {"codigo": codigo, "row": cupom}


def get_cupom(thread_id: int | str) -> dict | None:
    return _cupons.get(str(thread_id))


def clear_cupom(thread_id: int | str) -> None:
    _cupons.pop(str(thread_id), None)


def image_for(produto_key: str) -> str | None:
    produto = catalogo.produto(produto_key)
    return produto.imagem if produto else None


_CUSTOM_ID = {
    "pagar": "pagar_{pk}",
    "lote": "lote_{pk}",
    "cupom": "cupom_{pk}",
    "cancelar": "cancelar_pedido",
}


def _contexto(produto, quantidade: int, cupom: dict | None, estoque: int) -> dict:
    preco = store.preco(produto.id)
    total = round(preco * quantidade, 2)
    valor = f"**Valor:** {render.brl(total)}"
    if quantidade > 1:
        valor += f" ({quantidade} × {render.brl(preco)})"
    cupom_txt = ""
    if cupom:
        desconto = coupons.desconto(cupom["row"], total)
        valor = (
            f"**Valor:** ~~{render.brl(total)}~~ → **{render.brl(total - desconto)}**"
        )
        cupom_txt = (
            f"\n**{E.coupon} Cupom:** `{cupom['codigo']}` (-{render.brl(desconto)})"
        )
    return {
        "titulo": produto.nome + (f" × {quantidade}" if quantidade > 1 else ""),
        "preco": render.brl(preco),
        "quantidade": quantidade,
        "valor": valor,
        "cupom": cupom_txt,
        "cupom_codigo": cupom["codigo"] if cupom else "",
        "estoque": estoque,
        "garantia": f"\n**Garantia:** {produto.garantia}" if produto.garantia else "",
        "imagem": produto.imagem,
    }


def _botao(item: dict, produto_key: str, ctx: dict, cupom: dict | None) -> dict:
    label = item["label"]
    desabilitado = False
    if item["acao"] == "cupom" and cupom:
        label = item.get("label_com_cupom", label)
        desabilitado = True
    botao = render.botao(
        render.texto(label, ctx),
        _CUSTOM_ID[item["acao"]].format(pk=produto_key),
        item.get("style", 2),
        item.get("emoji"),
    )
    if desabilitado:
        botao["disabled"] = True
    return botao


def cart_payload(
    produto_key: str,
    quantidade: int = 1,
    *,
    cupom: dict | None = None,
    manual: bool = False,
    estoque: int = 0,
) -> dict:
    """Carrinho do produto: o layout vem do modelo de pedido (automático ou manual)."""
    produto = catalogo.produto(produto_key)
    manual = manual or estoque == 0 or produto.modelo.entrega == "manual"
    ctx = _contexto(produto, quantidade, cupom, estoque)
    ctx["pagamento"] = (
        produto.modelo.pagamento_manual if manual else produto.modelo.pagamento
    )
    layout = produto.modelo.carrinho["manual" if manual else "automatico"]

    inner: list[dict] = []
    for bloco in layout:
        if bloco["tipo"] == "botoes":
            inner.append(
                {
                    "type": 1,
                    "components": [
                        _botao(item, produto_key, ctx, cupom) for item in bloco["itens"]
                    ],
                }
            )
        else:
            inner += render.blocos([bloco], ctx)
    return {"flags": 1 << 15, "components": [{"type": 17, "components": inner}]}


async def open_cart(
    interaction: discord.Interaction, produto_key: str, *, manual: bool = False
) -> None:
    """Cria a thread do carrinho. Quem chama já fez defer da interação."""
    async with locks.resource(f"open:{interaction.user.id}:{produto_key}"):
        user = interaction.user
        if store.is_banned(user.id):
            await notify.ephemera(
                interaction, f"{E.wrong} Você está impedido de realizar compras."
            )
            return

        existente = store.open_thread(user.id, "compra", produto_key)
        if existente:
            try:
                thread = await context.channel(int(existente["thread_id"]))
                await notify.ephemera(
                    interaction,
                    f"{E.wrong} Você já tem um carrinho aberto para este produto: {thread.jump_url}",
                )
                return
            except discord.NotFound:
                store.forget_thread(existente["thread_id"])

        estoque = stock.count_for_user(produto_key, user.id)
        thread = await interaction.channel.create_thread(
            name=f"Pedido — {user.name}",
            type=discord.ChannelType.private_thread,
            invitable=False,
        )
        store.register_thread(thread.id, user.id, "compra", produto_key)
        await thread.add_user(user)
        await notify.add_team_to_thread(thread, excluir_id=user.id)
        await rest.send_message(
            thread.id, cart_payload(produto_key, manual=manual, estoque=estoque)
        )
        store.register_thread(thread.id, user.id, "compra", produto_key)

        await notify.ephemera(
            interaction,
            f"{E.cart} Seu carrinho foi criado!",
            view=link_button("Abrir carrinho", thread.jump_url),
        )
        if shelves.vitrine_e_seletor(produto_key):
            await shelves.refresh_for_product(produto_key)


async def close(thread_id: int | str) -> None:
    from ..data import orders

    async with locks.resource(f"cart:{thread_id}"):
        if orders.pending_in_thread(thread_id):
            raise RuntimeError("Cancele o PIX antes de fechar a thread")
        try:
            await (await context.channel(int(thread_id))).delete()
        except discord.NotFound:
            pass
        store.forget_thread(thread_id)
        scheduler.cancel(thread_id)
        clear_cupom(thread_id)
