"""Loja: vitrines, carrinhos, lote e cupom, pagamento PIX e pedidos de restock."""

import logging
from datetime import date

import discord
from discord.ext import commands

from ...config.settings import CORES
from ... import catalogo
from ...config import settings
from ...ui.components import static_view
from ...data import coupons, stock, store
from ...services import (
    avisos,
    carts,
    context,
    mercadopago,
    notify,
    payments,
    rest,
    shelves,
)
from ...services.payments import read_lote
from ..router import route
from ..security import authorize
from ...ui.emojis import E

log = logging.getLogger("store")

MAX_RESTOCK_DIA = settings.RESTOCK_PEDIDOS_DIA


def _zerados() -> list[discord.SelectOption]:
    return [
        discord.SelectOption(label=p.nome, value=p.id, description="Sem estoque")
        for p in catalogo.produtos()
        if p.modelo.estoque and stock.count_global(p.id) == 0
    ][:25]


def _sem_estoque_view(produto_key: str) -> discord.ui.View:
    view = static_view()
    canal = store.channel_for("PEDIDO_RESTOCK")
    if canal:
        try:
            url = f"https://discord.com/channels/{settings.GUILD_ID}/{canal}"
            view.add_item(
                discord.ui.Button(
                    label="Pedir Restock",
                    emoji=str(E.cardbox),
                    style=discord.ButtonStyle.link,
                    url=url,
                )
            )
        except (TypeError, ValueError):
            pass
    view.add_item(
        discord.ui.Button(
            label="Me avise quando chegar",
            emoji=str(E.bell),
            style=discord.ButtonStyle.secondary,
            custom_id=f"aviso_chegar:{produto_key}",
        )
    )
    return view


def _restock_fechar_view(thread_id: int) -> discord.ui.View:
    view = static_view()
    view.add_item(
        discord.ui.Button(
            label="Fechar thread",
            emoji=str(E.delete),
            style=discord.ButtonStyle.danger,
            custom_id=f"restock_fechar:{thread_id}",
        )
    )
    return view


class LoteModal(discord.ui.Modal, title="Escolher Quantidade"):
    qtd = discord.ui.TextInput(
        label="Quantidade", placeholder="1", min_length=1, max_length=3
    )

    def __init__(self, produto_key: str, estoque: int, message_id: int):
        super().__init__()
        self.produto_key = produto_key
        self.estoque = estoque
        self.message_id = message_id
        self.qtd.label = f"Quantidade (Max: {estoque})"

    async def on_submit(self, interaction: discord.Interaction) -> None:
        from ..security import authorize

        if not await authorize(interaction, "cart", produto=self.produto_key):
            return
        try:
            n = int(self.qtd.value)
            if not 1 <= n <= self.estoque:
                raise ValueError
        except ValueError:
            await interaction.response.send_message(
                f"{E.wrong} Quantidade inválida (1 a {self.estoque}).", ephemeral=True
            )
            return
        from ...database.connection import connection

        with connection() as conn:
            conn.execute(
                "UPDATE threads_ativas SET quantidade=? WHERE thread_id=?",
                (n, str(interaction.channel_id)),
            )
        await interaction.response.defer()
        payload = carts.cart_payload(
            self.produto_key,
            n,
            cupom=carts.get_cupom(interaction.channel_id),
            estoque=self.estoque,
        )
        await rest.edit_message(interaction.channel_id, self.message_id, payload)


class CupomModal(discord.ui.Modal, title="Aplicar Cupom"):
    codigo = discord.ui.TextInput(
        label="Código do Cupom", placeholder="GHOST10", min_length=1, max_length=20
    )

    def __init__(self, produto_key: str, message_id: int, quantidade: int):
        super().__init__()
        self.produto_key = produto_key
        self.message_id = message_id
        self.quantidade = quantidade

    async def on_submit(self, interaction: discord.Interaction) -> None:
        from ..security import authorize

        if not await authorize(interaction, "cart", produto=self.produto_key):
            return
        await interaction.response.defer(ephemeral=True)
        nome = self.codigo.value.strip().upper()
        cupom = coupons.get(nome)
        cargos = [r.id for r in getattr(interaction.user, "roles", [])]
        erro = _validar_cupom(
            cupom, interaction.user.id, store.preco(self.produto_key), cargos
        )
        if erro:
            await interaction.followup.send(erro, ephemeral=True)
            return
        from ...database.connection import connection

        with connection() as conn:
            quantidade = conn.execute(
                "SELECT quantidade FROM threads_ativas WHERE thread_id=?",
                (str(interaction.channel_id),),
            ).fetchone()[0]
        carts.set_cupom(interaction.channel_id, cupom["nome"], cupom)
        payload = carts.cart_payload(
            self.produto_key,
            quantidade,
            cupom={"codigo": cupom["nome"], "row": cupom},
            estoque=stock.count_for_user(self.produto_key, interaction.user.id),
        )
        await rest.edit_message(interaction.channel_id, self.message_id, payload)
        await interaction.followup.send(
            f"{E.correct} Cupom `{nome}` aplicado com sucesso!", ephemeral=True
        )


def _validar_cupom(
    cupom: dict | None, user_id: int, preco: float, cargos=()
) -> str | None:
    if not cupom:
        return f"{E.wrong} Cupom não encontrado."
    motivo = coupons.avaliar(cupom, user_id, preco, cargos)
    return f"{E.wrong} {motivo}" if motivo else None


def _hoje() -> str:
    return date.today().isoformat()


class Store(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── Vitrines (seleção e botões de compra) ─────────────────────────────────

    @route(prefix="select_produto_")
    @route(prefix="comprar_")
    async def _escolher_produto(
        self, interaction: discord.Interaction, cid: str
    ) -> None:
        valores = interaction.data.get("values")
        pk = valores[0] if valores else cid.split("_", 1)[1]
        produto = catalogo.produto(pk)
        if produto is None:
            log.warning("clique em vitrine antiga (id não existe mais): %s", cid)
            vitrine = shelves.vitrine_pelo_custom_id(cid)
            unicos = catalogo.produtos(vitrine=vitrine) if vitrine else []
            if vitrine:
                shelves.registrar_local(vitrine, interaction)
            if len(unicos) != 1:
                await interaction.response.defer()
                return
            await interaction.response.defer(ephemeral=True)
            await carts.open_cart(interaction, unicos[0].id)
            return
        shelves.registrar_local(produto.vitrine, interaction)
        if valores:
            # Seletor: qualquer escolha redesenha a mensagem inteira (zera o select e atualiza o estoque).
            await interaction.response.defer()
            await shelves.redesenhar_clicado(interaction, produto.vitrine)
            if produto.sem_estoque == "avisar" and stock.count_global(pk) == 0:
                await interaction.followup.send(
                    "😔 **Esse produto tá sem estoque agora...**\n-# Pede o restock ou ative o aviso abaixo!",
                    view=_sem_estoque_view(pk),
                    ephemeral=True,
                )
                return
            await carts.open_cart(interaction, pk)
            return
        if produto.sem_estoque == "avisar" and stock.count_global(pk) == 0:
            await interaction.response.send_message(
                "😔 **Esse produto tá sem estoque agora...**\n-# Pede o restock ou ative o aviso abaixo!",
                view=_sem_estoque_view(pk),
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)
        await carts.open_cart(interaction, pk)

    @route(prefix="aviso_chegar:")
    async def _aviso_chegar(self, interaction, cid):
        pk = cid.split(":", 1)[1]
        if avisos.inscrever(interaction.user.id, pk):
            texto = f"{E.correct} Você será avisado quando **{store.produto_nome(pk)}** voltar ao estoque."
        else:
            texto = f"{E.information} Você já está na lista de aviso de **{store.produto_nome(pk)}**."
        await interaction.response.send_message(texto, ephemeral=True)

    # ── Carrinho ──────────────────────────────────────────────────────────────

    @route(prefix="lote_")
    async def _lote(self, interaction, cid):
        if not await authorize(interaction, "cart"):
            return
        pk = cid[len("lote_") :]
        await interaction.response.send_modal(
            LoteModal(
                pk,
                stock.count_for_user(pk, interaction.user.id),
                interaction.message.id,
            )
        )

    @route(prefix="cupom_")
    async def _cupom(self, interaction, cid):
        if not await authorize(interaction, "cart"):
            return
        await interaction.response.send_modal(
            CupomModal(
                cid[len("cupom_") :],
                interaction.message.id,
                read_lote(interaction.message),
            )
        )

    @route(exact="cancelar_pedido")
    async def _cancelar_pedido(self, interaction, cid):
        if not await authorize(interaction, "cart"):
            return
        await interaction.response.defer()
        await carts.close(interaction.channel_id)

    # ── Pagamento PIX ─────────────────────────────────────────────────────────

    @route(prefix="pagar_")
    async def _pagar(self, interaction, cid):
        await interaction.response.defer()
        try:
            await payments.checkout(interaction, cid[len("pagar_") :])
        except mercadopago.PixError as exc:
            log.error("erro PIX: %s", exc)
            await interaction.followup.send(
                f"{E.wrong} Erro ao gerar pagamento: {exc}", ephemeral=True
            )

    @route(prefix="copiar_pix_")
    async def _copiar_pix(self, interaction, cid):
        try:
            await payments.copy_code(interaction, cid[len("copiar_pix_") :])
        except mercadopago.PixError:
            reply = (
                interaction.followup.send
                if interaction.response.is_done()
                else interaction.response.send_message
            )
            await reply(f"{E.wrong} Não consegui consultar esse PIX.", ephemeral=True)

    @route(prefix="entrega_concluida_")
    async def _entrega_concluida(self, interaction, cid):
        thread_id = cid[len("entrega_concluida_") :]
        dono = store.thread_owner(thread_id)
        if str(interaction.user.id) != str(dono) and not store.is_admin(
            interaction.user.id
        ):
            await interaction.response.send_message(
                f"{E.wrong} Só o cliente ou a equipe pode concluir.", ephemeral=True
            )
            return
        await interaction.response.defer()
        await notify.registrar(
            "entrega_manual_concluida",
            f"{E.correct} Entrega confirmada",
            CORES["sucesso"],
            [
                ("Thread", interaction.channel.name),
                ("Confirmado por", f"{interaction.user} ({interaction.user.id})"),
            ],
        )
        from ...database.connection import connection

        with connection() as conn:
            unfinished = conn.execute(
                "SELECT 1 FROM pedidos WHERE thread_id=? AND entrega_status IN ('pending','processing','manual') AND status IN ('pending','approved')",
                (str(thread_id),),
            ).fetchone()
        if unfinished:
            await interaction.followup.send(
                "A equipe precisa concluir a entrega restante primeiro.", ephemeral=True
            )
            return
        await carts.close(thread_id)

    @route(prefix="cancelar_pix_")
    async def _cancelar_pix(self, interaction, cid):
        await interaction.response.defer()
        await payments.cancel_by_user(interaction, cid[len("cancelar_pix_") :])

    # ── Restock ───────────────────────────────────────────────────────────────

    @route(exact="pedido_restock_btn")
    async def _restock_menu(self, interaction, cid):
        options = _zerados()
        if not options:
            await interaction.response.send_message(
                f"{E.correct} **Tudo em estoque!**\nNo momento todos os produtos estão disponíveis.",
                ephemeral=True,
            )
            return
        view = static_view()
        view.add_item(
            discord.ui.Select(
                placeholder="Selecione o produto sem estoque…",
                options=options,
                custom_id="pedido_restock_select",
                min_values=1,
                max_values=1,
            )
        )
        await interaction.response.send_message(
            "**Selecione abaixo o produto que você quer ver de volta no estoque.**",
            view=view,
            ephemeral=True,
        )

    @route(exact="pedido_restock_select")
    async def _restock_select(self, interaction, cid):
        pk = interaction.data["values"][0]
        await _delete_quietly(interaction)
        view = static_view()
        view.add_item(
            discord.ui.Button(
                label="Confirmar",
                style=discord.ButtonStyle.secondary,
                custom_id=f"pedido_restock_confirmar:{pk}",
            )
        )
        view.add_item(
            discord.ui.Button(
                label="Cancelar",
                style=discord.ButtonStyle.secondary,
                custom_id="pedido_restock_cancelar",
            )
        )
        await interaction.response.send_message(
            f"Tem certeza que quer pedir restock de **{store.produto_nome(pk)}**?",
            view=view,
            ephemeral=True,
        )

    @route(exact="pedido_restock_cancelar")
    async def _restock_cancelar(self, interaction, cid):
        await _delete_quietly(interaction)
        await interaction.response.send_message(
            f"{E.wrong} Pedido cancelado.", ephemeral=True
        )

    @route(prefix="pedido_restock_confirmar:")
    async def _restock_confirmar(self, interaction, cid):
        pk = cid.split(":", 1)[1]
        await _delete_quietly(interaction)
        await interaction.response.defer(ephemeral=True)
        if not await self._prepare_restock(interaction, pk):
            return
        view = static_view()
        view.add_item(
            discord.ui.Button(
                label="Receber notificação",
                style=discord.ButtonStyle.secondary,
                custom_id=f"pedido_restock_sim:{pk}",
            )
        )
        view.add_item(
            discord.ui.Button(
                label="Não, obrigado",
                style=discord.ButtonStyle.secondary,
                custom_id=f"pedido_restock_nao:{pk}",
            )
        )
        await interaction.followup.send(
            f"Quer ser avisado quando **{store.produto_nome(pk)}** voltar ao estoque?",
            view=view,
            ephemeral=True,
        )

    async def _prepare_restock(self, interaction: discord.Interaction, pk: str) -> bool:
        """Cria a thread e o pedido de restock. Retorna False se o pedido foi recusado."""
        nome = store.produto_nome(pk)
        existente = store.restock_pending(interaction.user.id, pk)
        if existente:
            if await _thread_has_customer(existente["thread_id"]):
                await interaction.followup.send(
                    f"{E.alert} Você já tem um pedido de restock pendente para **{nome}**.",
                    ephemeral=True,
                )
                return False
            await _discard_restock_thread(existente["thread_id"])
            store.delete_restock_pending(interaction.user.id, pk)

        if avisos.pedido_restock_limit_reached():
            await interaction.followup.send(
                f"{E.alert} Limite diário de pedidos de restock atingido ({MAX_RESTOCK_DIA}/dia). Tente amanhã.",
                ephemeral=True,
            )
            return False

        thread = await interaction.channel.create_thread(
            name=f"Restock • {nome}",
            type=discord.ChannelType.private_thread,
            auto_archive_duration=1440,
            invitable=False,
        )
        owner = settings.ADMIN_ID
        try:
            member = interaction.guild.get_member(
                owner
            ) or await interaction.guild.fetch_member(owner)
            await thread.add_user(member)
        except discord.HTTPException as exc:
            log.warning(
                "não foi possível adicionar o dono à thread de restock: %s", exc
            )
        await thread.send(
            f"{E.cardbox} **Pedido de Restock** — `{nome}`\n"
            f"{E.member} Cliente: {interaction.user.mention} (`{interaction.user.id}`)\n"
            f"<@{owner}>",
            view=_restock_fechar_view(thread.id),
        )
        try:
            store.create_restock(interaction.user.id, pk, thread.id)
        except Exception:
            await thread.delete()
            await interaction.followup.send(
                f"{E.alert} Você já tem um pedido de restock pendente para **{nome}**.",
                ephemeral=True,
            )
            return False
        return True

    @route(prefix="restock_fechar:")
    async def _restock_fechar(self, interaction, cid):
        if not store.is_admin(interaction.user.id):
            await interaction.response.send_message(
                f"{E.wrong} Sem permissão.", ephemeral=True
            )
            return
        thread_id = cid.split(":", 1)[1]
        store.close_restock_thread(thread_id)
        await interaction.response.defer()
        await _discard_restock_thread(thread_id)

    @route(prefix="pedido_restock_sim:")
    async def _restock_sim(self, interaction, cid):
        await self._restock_preferencia(interaction, cid.split(":", 1)[1], True)

    @route(prefix="pedido_restock_nao:")
    async def _restock_nao(self, interaction, cid):
        await self._restock_preferencia(interaction, cid.split(":", 1)[1], False)

    async def _restock_preferencia(self, interaction, pk: str, notificar: bool) -> None:
        await _delete_quietly(interaction)
        if notificar:
            avisos.inscrever(interaction.user.id, pk)
            await interaction.response.send_message(
                f"{E.correct} Notificação ativada para **{store.produto_nome(pk)}**. "
                f"Você será avisado assim que o produto voltar ao estoque.",
                ephemeral=True,
            )
        else:
            await interaction.response.defer(ephemeral=True)


async def _delete_quietly(interaction: discord.Interaction) -> None:
    try:
        await interaction.message.delete()
    except discord.HTTPException:
        pass


async def _thread_has_customer(thread_id: str) -> bool:
    try:
        thread = await context.channel(int(thread_id))
    except discord.HTTPException:
        return False
    members = await thread.fetch_members()
    return any(
        m.id != context.bot().user.id
        and not getattr(thread.guild.get_member(m.id), "bot", False)
        for m in members
    )


async def _discard_restock_thread(thread_id: str) -> None:
    try:
        await (await context.channel(int(thread_id))).delete()
    except discord.HTTPException:
        pass


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Store(bot))
