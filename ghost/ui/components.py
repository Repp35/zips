"""Componentes reutilizados em mensagens: botões de PIX, tickets e links."""

import discord
from .emojis import E


def static_view() -> discord.ui.View:
    """View só com botões de custom_id (roteados pelo Router). Não entra no store do discord.py."""
    view = discord.ui.View(timeout=None)
    view.stop()
    return view


def link_button(label: str, url: str) -> discord.ui.View:
    view = static_view()
    view.add_item(
        discord.ui.Button(label=label, url=url, style=discord.ButtonStyle.link)
    )
    return view


def pix_view(payment_id: str) -> discord.ui.View:
    view = static_view()
    view.add_item(
        discord.ui.Button(
            label="Copiar código PIX",
            style=discord.ButtonStyle.primary,
            emoji="📋",
            custom_id=f"copiar_pix_{payment_id}",
        )
    )
    view.add_item(
        discord.ui.Button(
            label="Cancelar",
            style=discord.ButtonStyle.danger,
            emoji=f"{E.delete}",
            custom_id=f"cancelar_pix_{payment_id}",
        )
    )
    return view


def manual_delivery_view(pedido_id: int) -> discord.ui.View:
    view = static_view()
    view.add_item(
        discord.ui.Button(
            label="Concluído",
            style=discord.ButtonStyle.success,
            emoji=f"{E.correct}",
            custom_id=f"ticket_concluido_{pedido_id}",
        )
    )
    view.add_item(
        discord.ui.Button(
            label="Fechar e Salvar",
            style=discord.ButtonStyle.danger,
            emoji=f"{E.delete}",
            custom_id="ticket_fechar",
        )
    )
    return view
