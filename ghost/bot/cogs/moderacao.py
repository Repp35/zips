"""Moderação e administração: /timeout, /ban, /antiraid, /reembolso e /dashboard, cada um com variação `>`.

Cada comando tem uma função compartilhada (timeout_acao, ban_acao, reembolso_acao, painel_dashboard) que a barra
e o prefixo chamam. Níveis: /timeout é [ admin ]; /ban é [ admin ]; /antiraid, /reembolso e /dashboard são [ dono ].
Respostas com dinheiro ou painel de configuração vão por DM quando vêm do prefixo; na barra são efêmeras.
"""

import logging
import re
from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands

from ..checks import admin_only as _admin_only_check
from ...data import store

from ...ui import layout
from ...config import settings
from ...catalogo import render
from ...data import orders
from ...ui.emojis import E
from ...services import context, mercadopago, notify, rest
from ...config.settings import CORES
from ..checks import admin_only
from ..router import route
from ..secoes import antiraid, dashboard

log = logging.getLogger("moderacao")

ACOES_TIMEOUT = ("add", "remove", "editar")
ACOES_BAN = ("add", "remove")
MAX_TIMEOUT_S = 28 * 86400  # limite do Discord
_TEMPO = re.compile(r"(\d+)\s*([smhd])", re.IGNORECASE)
_UNIDADE = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_reembolsando: set[int] = (
    set()
)  # pedidos com reembolso em andamento (evita dois cliques no mesmo pedido)


# ── Permissão e entrada ───────────────────────────────────────────────────────


def _dono(user_id: int) -> bool:
    return user_id == settings.ADMIN_ID


def _dono_only():
    async def check(interaction: discord.Interaction) -> bool:
        if _dono(interaction.user.id):
            return True
        await interaction.response.send_message(
            f"{E.wrong} Só o dono pode usar este comando.", ephemeral=True
        )
        return False

    return app_commands.check(check)


def _guild_de(interaction: discord.Interaction) -> discord.Guild | None:
    """Em DM a interação não tem servidor; nesse caso usa o servidor configurado."""
    if interaction.guild is not None:
        return interaction.guild
    try:
        return context.bot().get_guild(settings.GUILD_ID)
    except RuntimeError:
        return None


def _resolver(texto: str, guild: discord.Guild | None) -> int | None:
    """ID, @menção ou nome (username ou apelido) de um membro do servidor."""
    alvo = (texto or "").strip()
    marca = re.fullmatch(r"<@!?(\d+)>", alvo)
    if marca:
        return int(marca.group(1))
    alvo = alvo.lstrip("@").strip()
    if alvo.isdigit():
        return int(alvo)
    if guild is None or not alvo:
        return None
    membro = guild.get_member_named(alvo)
    return membro.id if membro else None


def _bot_id() -> int | None:
    try:
        usuario = context.bot().user
    except RuntimeError:
        return None
    return usuario.id if usuario is not None else None


def _protegido(uid: int) -> bool:
    """Equipe e o próprio bot não recebem timeout nem ban por estes comandos."""
    return uid == _bot_id() or store.is_admin(uid) or store.is_staff(uid)


async def _membro(guild: discord.Guild, uid: int) -> discord.Member | None:
    membro = guild.get_member(uid)
    if membro is None:
        try:
            membro = await guild.fetch_member(uid)
        except discord.NotFound:
            return None
    return membro


def _duracao(segundos: int) -> str:
    partes = []
    for nome, tamanho in (("d", 86400), ("h", 3600), ("min", 60), ("s", 1)):
        quantidade, segundos = divmod(segundos, tamanho)
        if quantidade:
            partes.append(f"{quantidade}{nome}")
    return " ".join(partes) or "0s"


def _tempo_segundos(texto: str) -> int | None:
    """'10m', '2h', '7d', '30 s'. Número e unidade obrigatórios; zero não vale."""
    casou = _TEMPO.fullmatch((texto or "").strip())
    if not casou:
        return None
    segundos = int(casou.group(1)) * _UNIDADE[casou.group(2).lower()]
    return segundos if segundos > 0 else None


# ── Funções compartilhadas: barra e prefixo chamam estas ──────────────────────


async def timeout_acao(
    guild: discord.Guild | None, autor, acao: str, usuario: str, tempo: str
) -> str:
    """Aplica (add/editar) ou remove (remove) timeout. Devolve o texto da resposta."""
    if guild is None:
        return f"{E.wrong} Use este comando dentro do servidor."
    if acao not in ACOES_TIMEOUT:
        return f"{E.wrong} Ação inválida: use add, remove ou editar."
    uid = _resolver(usuario, guild)
    if uid is None:
        return f"{E.wrong} Não encontrei esse usuário. Use o ID, @ ou o nome exato."
    if _protegido(uid):
        return f"{E.wrong} Não aplico timeout na equipe nem no bot."
    membro = await _membro(guild, uid)
    if membro is None:
        return f"{E.wrong} <@{uid}> não está no servidor."
    motivo = f"Timeout ({acao}) por {autor}"
    try:
        if acao == "remove":
            await membro.timeout(None, reason=motivo)
            return f"{E.correct} Timeout removido de <@{uid}>."
        segundos = _tempo_segundos(tempo)
        if segundos is None:
            return f"{E.wrong} Tempo inválido. Use número e s, m, h ou d (ex.: 10m, 2h, 7d)."
        if segundos > MAX_TIMEOUT_S:
            return f"{E.wrong} O máximo é 28 dias (limite do Discord)."
        await membro.timeout(timedelta(seconds=segundos), reason=motivo)
    except discord.HTTPException as exc:
        log.warning("timeout em %s falhou: %s", uid, exc)
        return f"{E.wrong} O Discord recusou a ação (o cargo do alvo pode estar acima do bot)."
    verbo = "Timeout aplicado a" if acao == "add" else "Tempo de timeout alterado para"
    return f"{E.clock} {verbo} <@{uid}>: {_duracao(segundos)}."


async def ban_acao(guild: discord.Guild | None, autor, acao: str, usuario: str) -> str:
    """Bane (add) ou desbane (remove). Quem não está no servidor só pode ser banido pelo ID."""
    if guild is None:
        return f"{E.wrong} Use este comando dentro do servidor."
    if acao not in ACOES_BAN:
        return f"{E.wrong} Ação inválida: use add ou remove."
    uid = _resolver(usuario, guild)
    if uid is None:
        return f"{E.wrong} Não encontrei esse usuário. No servidor, use o ID, @ ou o nome; fora dele, só o ID."
    motivo = f"Ban ({acao}) por {autor}"
    try:
        if acao == "add":
            if _protegido(uid):
                return f"{E.wrong} Não bano a equipe nem o bot."
            membro = guild.get_member(uid)
            await guild.ban(
                membro or discord.Object(id=uid),
                reason=motivo,
                delete_message_seconds=0,
            )
            return f"{E.correct} <@{uid}> banido" + (
                "" if membro else " pelo ID (não estava no servidor)."
            )
        await guild.unban(discord.Object(id=uid), reason=motivo)
        return f"{E.correct} Ban de `{uid}` removido."
    except discord.NotFound:
        return f"{E.wrong} `{uid}` não está banido."
    except discord.HTTPException as exc:
        log.warning("ban em %s falhou: %s", uid, exc)
        return f"{E.wrong} O Discord recusou a ação."


async def reembolso_acao(compra: str, autor) -> str:
    """Reembolso total de uma compra aprovada. `compra` é o número do pedido (#N nos logs) ou o ID do pagamento."""
    texto = (compra or "").strip()
    if not texto.isdigit() or len(texto) > 18:
        return f"{E.wrong} Informe o número da compra (ex.: 42)."
    pedido = orders.get(int(texto)) or orders.get_by_payment(texto)
    if pedido is None:
        return f"{E.wrong} Compra não encontrada."
    pid = pedido["id"]
    if pedido["status"] == orders.STATUS_REEMBOLSADO:
        return f"{E.alert} A compra #{pid} já foi reembolsada."
    if pedido["status"] != orders.STATUS_APROVADO:
        return f"{E.wrong} Só dá para reembolsar compra aprovada. A compra #{pid} está: {pedido['status']}."
    pagamento = pedido["payment_id"] or ""
    if not pagamento.isdigit():
        return f"{E.wrong} A compra #{pid} foi entregue à mão, sem pagamento no Mercado Pago. Não há reembolso automático."
    if pid in _reembolsando:
        return f"{E.alert} O reembolso da compra #{pid} já está em andamento."
    _reembolsando.add(pid)
    try:
        resposta = await mercadopago.refund(pagamento)
    except mercadopago.PixError as exc:
        return f"{E.wrong} O Mercado Pago recusou o reembolso: {exc}"
    finally:
        _reembolsando.discard(pid)
    if resposta.get("status") != "approved":
        return (
            f"{E.alert} O Mercado Pago respondeu `{resposta.get('status')}`. A compra #{pid} continua aprovada "
            "aqui; confira no painel do Mercado Pago."
        )
    if not orders.marcar_reembolsado(pid):
        log.error(
            "reembolso da compra %s feito no Mercado Pago, mas o pedido não estava aprovado aqui",
            pid,
        )
    valor = float(pedido["valor"])
    await notify.registrar(
        "reembolso",
        f"{E.receipt} Reembolso feito",
        CORES["aviso"],
        [
            ("Pedido", f"#{pid}"),
            (
                "Cliente",
                f"<@{pedido['discord_user_id']}> ({pedido['discord_user_id']})",
            ),
            ("Produto", store.produto_nome(pedido["produto_key"])),
            ("Valor", render.brl(valor)),
            ("Pagamento MP", pagamento),
            ("Por", f"{autor} ({autor.id})"),
        ],
    )
    return f"{E.correct} Compra #{pid} reembolsada: {render.brl(valor)}."


async def painel_dashboard(guild: discord.Guild | None) -> dict:
    """Mensagem do /dashboard (mesmo conteúdo do Gerenciar > Dashboard, sem Voltar). Efêmera."""
    vendas, servidor = await dashboard.conteudo(guild)
    return layout.mensagem(
        layout.cartao(
            layout.titulo(f"{E.basket} Dashboard"),
            layout.texto(vendas),
            layout.separador(),
            layout.texto(servidor),
            layout.separador(),
            layout.linha(
                render.botao("Atualizar", "dashboard:atualizar", 2, "@reload")
            ),
            cor=CORES["neutro"],
        ),
        efemera=True,
    )


async def _privado(ctx: commands.Context, payload: dict) -> None:
    """Manda a resposta por DM e confirma no canal. Sem DM aberta, avisa no canal."""
    try:
        await rest.send_dm(ctx.author, payload)
    except (RuntimeError, discord.HTTPException):
        await ctx.send(
            f"{E.alert} Não consegui te mandar DM. Abra suas mensagens privadas e tente de novo.",
            delete_after=15,
        )
        return
    await ctx.send(f"{E.correct} Resposta enviada na sua DM.", delete_after=10)


def _so_texto(texto: str) -> dict:
    return {"content": texto}


class Moderacao(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── /timeout [ admin ] ────────────────────────────────────────────────────

    @app_commands.command(name="timeout", description="[ admin ]")
    @admin_only()
    @app_commands.describe(
        acao="add, remove ou editar",
        usuario="ID, @ ou nome",
        tempo="10m, 2h, 7d (máx. 28d)",
    )
    @app_commands.choices(
        acao=[app_commands.Choice(name=a, value=a) for a in ACOES_TIMEOUT]
    )
    async def timeout_cmd(
        self, interaction: discord.Interaction, acao: str, usuario: str, tempo: str = ""
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        texto = await timeout_acao(
            interaction.guild, interaction.user, acao, usuario, tempo
        )
        await interaction.edit_original_response(content=texto)

    @commands.command(name="timeout")
    async def timeout_prefixo(
        self, ctx: commands.Context, acao: str = "", usuario: str = "", tempo: str = ""
    ) -> None:
        if not store.is_admin(ctx.author.id):
            return
        texto = await timeout_acao(ctx.guild, ctx.author, acao.lower(), usuario, tempo)
        await ctx.send(texto, delete_after=20)

    @commands.Cog.listener()
    async def on_voice_state_update(
        self, member: discord.Member, antes, depois
    ) -> None:
        """Membro com timeout não entra em call: se tentar, é desconectado na hora."""
        if depois.channel is not None and not member.bot and member.is_timed_out():
            try:
                await member.move_to(None)
            except discord.HTTPException:
                pass

    # ── /ban [ admin ] ─────────────────────────────────────────────────────────

    @app_commands.command(name="ban", description="[ admin ]")
    @_admin_only_check()
    @app_commands.describe(
        acao="add ou remove", usuario="ID, @ ou nome (fora do servidor, só ID)"
    )
    @app_commands.choices(
        acao=[app_commands.Choice(name=a, value=a) for a in ACOES_BAN]
    )
    async def ban_cmd(
        self, interaction: discord.Interaction, acao: str, usuario: str
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        texto = await ban_acao(interaction.guild, interaction.user, acao, usuario)
        await interaction.edit_original_response(content=texto)

    @commands.command(name="ban")
    async def ban_prefixo(
        self, ctx: commands.Context, acao: str = "", usuario: str = ""
    ) -> None:
        if not store.is_admin(ctx.author.id):
            return
        texto = await ban_acao(ctx.guild, ctx.author, acao.lower(), usuario)
        await ctx.send(texto, delete_after=20)

    # ── /reembolso [ dono ] ───────────────────────────────────────────────────

    @app_commands.command(name="reembolso", description="[ dono ]")
    @_dono_only()
    @app_commands.describe(compra="Número da compra (#N nos logs)")
    async def reembolso_cmd(
        self, interaction: discord.Interaction, compra: int
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        texto = await reembolso_acao(str(compra), interaction.user)
        await interaction.edit_original_response(content=texto)

    @commands.command(name="reembolso")
    async def reembolso_prefixo(self, ctx: commands.Context, compra: str = "") -> None:
        if not _dono(ctx.author.id):
            return
        await _privado(ctx, _so_texto(await reembolso_acao(compra, ctx.author)))

    # ── /dashboard [ dono ] ───────────────────────────────────────────────────

    @app_commands.command(name="dashboard", description="[ dono ]")
    @_dono_only()
    async def dashboard_cmd(self, interaction: discord.Interaction) -> None:
        await rest.responder(
            interaction, await painel_dashboard(_guild_de(interaction))
        )

    @commands.command(name="dashboard")
    async def dashboard_prefixo(self, ctx: commands.Context) -> None:
        if not _dono(ctx.author.id):
            return
        await _privado(ctx, await painel_dashboard(ctx.guild))

    @route(exact="dashboard:atualizar")
    async def _dashboard_atualizar(
        self, interaction: discord.Interaction, cid: str
    ) -> None:
        if not _dono(interaction.user.id):
            await interaction.response.send_message(
                f"{E.wrong} Só o dono pode ver o dashboard.", ephemeral=True
            )
            return
        await rest.responder(
            interaction, await painel_dashboard(_guild_de(interaction)), editar=True
        )

    # ── /antiraid [ dono ] ────────────────────────────────────────────────────

    @app_commands.command(name="antiraid", description="[ dono ]")
    @_dono_only()
    async def antiraid_cmd(self, interaction: discord.Interaction) -> None:
        await rest.responder(interaction, antiraid.menu(avulso=True))

    @commands.command(name="antiraid")
    async def antiraid_prefixo(self, ctx: commands.Context) -> None:
        if not _dono(ctx.author.id):
            return
        await _privado(ctx, antiraid.menu(avulso=True))

    @route(prefix="ger:antiraid:")
    async def _antiraid_componente(
        self, interaction: discord.Interaction, cid: str
    ) -> None:
        """Painel do anti-raid. Tem prefixo mais longo que o do /gerenciar, então o roteador chega aqui primeiro."""
        partes = cid.split(":")
        await antiraid.tratar(
            interaction, partes[2] if len(partes) > 2 else "", partes[3:]
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Moderacao(bot))
