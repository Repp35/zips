"""Autorizações reavaliadas no envio, inclusive em modais já abertos."""

from ..config import settings
from ..data import store


async def authorize(interaction, level="owner", *, produto=None):
    uid = interaction.user.id
    guild_id = getattr(interaction, "guild_id", None)
    valid = guild_id in (None, settings.GUILD_ID)
    if level == "owner":
        valid = valid and uid == settings.ADMIN_ID
    elif level == "admin":
        valid = valid and store.is_admin(uid)
    elif level == "cart":
        state = store.ticket_state(interaction.channel_id)
        valid = (
            valid
            and state is not None
            and state["tipo"] == "compra"
            and str(uid) == state["user_id"]
        )
        if valid and produto is not None:
            from ..database.connection import connection

            with connection() as conn:
                row = conn.execute(
                    "SELECT produto_key FROM threads_ativas WHERE thread_id=?",
                    (str(interaction.channel_id),),
                ).fetchone()
            valid = row is not None and row["produto_key"] == produto
        if valid:
            from ..data import orders

            valid = not orders.pending_in_thread(interaction.channel_id)
    if valid:
        return True
    reply = (
        interaction.followup.send
        if interaction.response.is_done()
        else interaction.response.send_message
    )
    await reply(
        "Sem permissão, carrinho já pago ou operação não disponível neste canal.",
        ephemeral=True,
    )
    return False
