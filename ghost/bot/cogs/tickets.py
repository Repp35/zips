"""Tickets de suporte: abertura por vitrine, escolha de produto na thread e ações da equipe."""

import logging

import discord
from discord.ext import commands

from ...config.settings import CORES
from ... import catalogo
from ...ui import layout
from ...catalogo import render
from ...ui.components import static_view
from ...data import orders, store
from ...services import carts, context, notify, rest, shelves, locks
from ..router import route
from ...ui.emojis import E

log = logging.getLogger("tickets")


def _tipo(tipo: str) -> tuple[str, str]:
    """Nome e ícone do tipo de ticket, lidos das opções da vitrine de suporte."""
    opcao = next(
        o for o in catalogo.vitrine("SUPORTE").dados["opcoes"] if o["value"] == tipo
    )
    icone = opcao["emoji"][1:] if opcao["emoji"].startswith("@") else None
    return opcao["label"], str(getattr(E, icone)) if icone else opcao["emoji"]


def _product_options(user_id: int) -> list[dict]:
    keys = orders.recent_products(user_id)
    options = [
        {"label": store.produto_nome(k), "value": f"produto_ticket_{k}"} for k in keys
    ]
    return options or [
        {"label": "Outro / Não listado", "value": "produto_ticket_outro"}
    ]


def _ticket_payload(
    nome: str, emoji, thread_id: int | str, tipo: str, user_id: int
) -> dict:
    return layout.mensagem(
        layout.cartao(
            layout.imagem(catalogo.vitrine("SUPORTE").dados["imagem"]),
            layout.titulo(f"{emoji} {nome}"),
            layout.texto("**Selecione o produto ou o motivo do seu ticket abaixo.**"),
            layout.linha(
                layout.seletor(
                    f"ticket_produto_select_{thread_id}_{tipo}",
                    "Selecione o produto ou motivo do ticket",
                    _product_options(user_id),
                )
            ),
            cor=CORES["info"],
        )
    )


def _card_pedido(thread_id: int | str) -> dict:
    """Card do ticket depois da escolha: motivo, valor (se houver) e o estado de atendimento."""
    estado = store.ticket_state(thread_id)
    escolha = estado["ticket_escolha"]
    produto_key = None if escolha in (None, "outro") else escolha
    rotulo = (
        store.produto_nome(produto_key) if produto_key else "Outro / Problema geral"
    )
    campos = [("Produto", f"`{rotulo}`")]
    if produto_key:
        ultimo = orders.last_approved(int(estado["user_id"]), produto_key)
        if ultimo:
            campos.append(("Valor", f"R$ {ultimo['valor']:.2f}"))
    blocos = [
        layout.titulo(f"{E.receipt} Pedido selecionado"),
        layout.texto(
            f"Ticket para tratar sobre **{rotulo}**.\nCliente: <@{estado['user_id']}>"
        ),
        layout.separador(),
        layout.texto(layout.pares(campos)),
    ]
    fechar = render.botao("Fechar e Salvar", "ticket_fechar", 4, "@delete")
    if estado["assumido_por"]:
        blocos.append(
            layout.texto(f"{E.correct} Assumido por <@{estado['assumido_por']}>")
        )
        blocos.append(layout.linha(fechar))
    else:
        blocos.append(
            layout.linha(
                render.botao("Assumir ticket", "ticket_assumir", 1, "@ticket"), fechar
            )
        )
    return layout.mensagem(layout.cartao(*blocos, cor=CORES["info"]))


class Tickets(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @route(exact="suporte_tipo_select")
    async def _abrir_ticket(self, interaction: discord.Interaction, cid: str) -> None:
        async with locks.resource(f"ticket:{interaction.user.id}"):
            tipo = interaction.data["values"][0]
            nome, emoji = _tipo(tipo)
            await interaction.response.defer(ephemeral=True)

            existente = store.open_thread(interaction.user.id, tipo)
            if existente:
                try:
                    thread = await context.channel(int(existente["thread_id"]))
                    await interaction.followup.send(
                        f"{E.wrong} Você já tem um ticket de suporte aberto: {thread.jump_url}",
                        ephemeral=True,
                    )
                    return
                except discord.NotFound:
                    store.forget_thread(existente["thread_id"])

            thread = await interaction.channel.create_thread(
                name=f"{nome} — {interaction.user.name}",
                type=discord.ChannelType.private_thread,
                invitable=False,
            )
            await interaction.followup.send(
                f"{E.headset} Ticket criado!",
                view=_link(thread.jump_url),
                ephemeral=True,
            )
            store.register_thread(thread.id, interaction.user.id, tipo)
            await thread.add_user(interaction.user)
            await notify.add_team_to_thread(thread, excluir_id=interaction.user.id)
            await rest.send_message(
                thread.id,
                _ticket_payload(nome, emoji, thread.id, tipo, interaction.user.id),
            )
            await notify.registrar(
                "ticket_aberto",
                f"{E.information} Ticket aberto",
                CORES["info"],
                [
                    ("Usuário", f"{interaction.user} ({interaction.user.id})"),
                    ("Tipo", nome),
                    ("Thread", thread.name),
                ],
            )
            await shelves.refresh("SUPORTE")

    @route(prefix="ticket_produto_select_")
    async def _escolher_produto(
        self, interaction: discord.Interaction, cid: str
    ) -> None:
        thread_id, _, tipo = cid[len("ticket_produto_select_") :].rpartition("_")
        if str(interaction.channel_id) != thread_id or str(interaction.user.id) != str(
            store.thread_owner(thread_id)
        ):
            await interaction.response.send_message(
                "Só o cliente escolhe o motivo do próprio ticket.", ephemeral=True
            )
            return
        escolha = interaction.data["values"][0]
        await interaction.response.defer()
        produto_key = (
            None
            if escolha == "produto_ticket_outro"
            else escolha[len("produto_ticket_") :]
        )
        if not store.choose_ticket_reason(thread_id, produto_key or "outro"):
            return

        try:
            await interaction.message.delete()
        except discord.HTTPException:
            pass
        await rest.send_message(interaction.channel_id, _card_pedido(thread_id))
        rotulo = (
            store.produto_nome(produto_key) if produto_key else "Outro / Problema geral"
        )
        try:
            await interaction.channel.edit(
                name=f"{tipo.capitalize()} | {rotulo} — {interaction.user.name}"[:100]
            )
        except discord.HTTPException:
            pass

    @route(exact="ticket_assumir")
    async def _assumir(self, interaction: discord.Interaction, cid: str) -> None:
        if not store.is_admin(interaction.user.id):
            await interaction.response.send_message(
                f"{E.wrong} Sem permissão.", ephemeral=True
            )
            return
        if not store.claim_ticket(interaction.channel_id, interaction.user.id):
            estado = store.ticket_state(interaction.channel_id)
            await interaction.response.send_message(
                f"{E.alert} Esse ticket já foi assumido por <@{estado['assumido_por']}>.",
                ephemeral=True,
            )
            return
        await rest.responder(
            interaction, _card_pedido(interaction.channel_id), editar=True
        )

    @route(exact="ticket_fechar")
    async def _fechar(self, interaction: discord.Interaction, cid: str) -> None:
        if not store.is_admin(interaction.user.id):
            await interaction.response.send_message(
                f"{E.wrong} Sem permissão.", ephemeral=True
            )
            return
        await interaction.response.defer()
        await notify.registrar(
            "ticket_fechado",
            f"{E.save} Ticket Encerrado",
            CORES["info"],
            [
                ("Thread", interaction.channel.name),
                ("Fechado por", str(interaction.user)),
            ],
        )
        await carts.close(interaction.channel_id)

    @route(prefix="ticket_concluido_")
    async def _concluir_manual(
        self, interaction: discord.Interaction, cid: str
    ) -> None:
        if not store.is_admin(interaction.user.id):
            await interaction.response.send_message(
                f"{E.wrong} Sem permissão.", ephemeral=True
            )
            return
        try:
            pedido_id = int(cid[len("ticket_concluido_") :])
        except ValueError:
            await interaction.response.send_message(
                f"{E.wrong} Pedido inválido.", ephemeral=True
            )
            return
        await interaction.response.defer()
        pedido = orders.get(pedido_id)
        if (
            pedido is None
            or pedido["status"] != "approved"
            or pedido["thread_id"] != str(interaction.channel_id)
            or pedido["entrega_status"] == "completed"
        ):
            await interaction.followup.send(
                f"{E.alert} Pedido não encontrado (ou já não está aprovado).",
                ephemeral=True,
            )
            return

        orders.set_delivery(pedido_id, "completed")
        produto_key, valor = pedido["produto_key"], float(pedido["valor"])
        user = await context.user(int(pedido["discord_user_id"]))
        await interaction.channel.send(
            f"{E.correct} Entrega manual confirmada por {interaction.user.mention}. "
            f"Vou registrar a venda e fechar a thread."
        )
        if orders.claim_sale(pedido_id):
            await notify.send_sale(user, produto_key, valor)
        await shelves.refresh_for_product(produto_key)
        if interaction.guild:
            await notify.sync_client_roles(user.id, interaction.guild)
        await notify.registrar(
            "entrega_manual_concluida",
            f"{E.correct} Pedido Concluído (Entrega Manual)",
            CORES["sucesso"],
            [
                ("Thread", interaction.channel.name),
                ("Pedido", f"#{pedido_id}"),
                ("Produto", store.produto_nome(produto_key)),
                ("Valor", f"R$ {valor:.2f}"),
                ("Concluído por", str(interaction.user)),
            ],
        )
        await carts.close(interaction.channel_id)


def _link(url: str) -> discord.ui.View:
    view = static_view()
    view.add_item(
        discord.ui.Button(label="Abrir ticket", url=url, style=discord.ButtonStyle.link)
    )
    return view


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Tickets(bot))
