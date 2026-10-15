"""/backup [ dono ], com variação `>`. Tudo de backup fica aqui, numa ação só:

- criar (padrão): cria um backup agora, com nome opcional, e registra no log.
- restaurar: escolhe um backup e pede confirmação antes de restaurar.
- apagar: escolhe um backup e pede confirmação antes de apagar. Registra no log.

O backup diário é silencioso: não aparece no log.
"""

import json
import logging

import discord
from discord import app_commands
from discord.ext import commands

from ...config import settings
from ...ui.emojis import E
from ...services import backup, notify, palavroes, raid
from ...data import store
from ...config.settings import CORES
from ..router import route

log = logging.getLogger("backup_cmds")


def _dono(user_id: int) -> bool:
    return user_id == settings.ADMIN_ID


def _pode_untimeout(user) -> bool:
    """Dono, ou quem tem permissão de administrador no servidor."""
    if user.id == settings.ADMIN_ID:
        return True
    perms = getattr(user, "guild_permissions", None)
    return bool(perms and perms.administrator)


def _rotulo(dados: dict) -> str:
    base = f"{dados['servidor']['nome']} · {dados['criado_em'].replace('T', ' ')}"
    return f"{base} · {dados['nome']}" if dados.get("nome") else base


def _achados(termo: str) -> list[tuple]:
    return [(a, d) for a, d in backup.listar() if termo.lower() in _rotulo(d).lower()]


def _confirmacao_restaurar(arquivo: str) -> discord.ui.View:
    from ...ui.components import static_view

    view = static_view()
    view.add_item(
        discord.ui.Button(
            label="Restaurar",
            style=discord.ButtonStyle.danger,
            custom_id=f"bkp:ok:{arquivo}",
        )
    )
    view.add_item(
        discord.ui.Button(
            label="Cancelar", style=discord.ButtonStyle.secondary, custom_id="bkp:no"
        )
    )
    return view


def _confirmacao_apagar(arquivo: str) -> discord.ui.View:
    from ...ui.components import static_view

    view = static_view()
    view.add_item(
        discord.ui.Button(
            label="Apagar",
            style=discord.ButtonStyle.danger,
            custom_id=f"bkp:apagar:{arquivo}",
        )
    )
    view.add_item(
        discord.ui.Button(
            label="Cancelar", style=discord.ButtonStyle.secondary, custom_id="bkp:no"
        )
    )
    return view


async def _autocomplete_backup(
    interaction: discord.Interaction, atual: str
) -> list[app_commands.Choice[str]]:
    escolhas = []
    for arq, dados in backup.listar():
        rotulo = _rotulo(dados)
        if (atual or "").lower() in rotulo.lower():
            escolhas.append(app_commands.Choice(name=rotulo[:100], value=arq.name))
    return escolhas[:25]


class Backups(commands.Cog):
    grupo_restauracao = app_commands.Group(name="restauracao", description="[ dono ]")

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def _apagar_confirmacao_clicada(self, interaction: discord.Interaction) -> None:
        """Apaga o prompt que acabou de ser resolvido; não deixa botões velhos no canal."""
        mensagem = interaction.message
        if mensagem is None:
            return
        try:
            await mensagem.delete()
        except discord.NotFound:
            pass
        except discord.HTTPException:
            log.exception("não consegui apagar a mensagem de confirmação clicada")

    @grupo_restauracao.command(name="parar", description="[ dono ] Para a restauração atual")
    async def restauracao_parar(self, interaction: discord.Interaction) -> None:
        if not _dono(interaction.user.id):
            await interaction.response.send_message(f"{E.wrong} Só o dono pode usar este comando.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await interaction.followup.send(await backup.parar(), ephemeral=True)

    @grupo_restauracao.command(name="continuar", description="[ dono ] Retoma ou reinicia a reconciliação pendente")
    async def restauracao_continuar(self, interaction: discord.Interaction) -> None:
        if not _dono(interaction.user.id):
            await interaction.response.send_message(f"{E.wrong} Só o dono pode usar este comando.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await interaction.followup.send(await backup.continuar(interaction.guild), ephemeral=True)

    @grupo_restauracao.command(name="estado", description="[ dono ] Mostra a fase e o checkpoint atual")
    async def restauracao_estado(self, interaction: discord.Interaction) -> None:
        if not _dono(interaction.user.id):
            await interaction.response.send_message(f"{E.wrong} Só o dono pode usar este comando.", ephemeral=True)
            return
        await interaction.response.send_message(backup.resumo_restauracao(), ephemeral=True)

    async def _criar(self, guild: discord.Guild | None, autor, nome: str | None) -> str:
        if guild is None:
            return f"{E.wrong} Só funciona dentro do servidor."
        try:
            caminho = await backup.criar(guild, motivo="manual", nome=nome)
        except RuntimeError as exc:
            log.warning("backup não criado: %s", exc)
            return f"{E.wrong} Backup cancelado: {exc}"
        except Exception:
            log.exception("backup falhou com erro inesperado")
            return f"{E.wrong} Backup falhou com erro inesperado. Veja o log."
        if caminho is None:
            return f"{E.alert} Não consegui criar o backup agora (raid em andamento). Tente depois."
        dados = next(d for a, d in backup.listar() if a == caminho)
        await notify.registrar(
            "backup_criado",
            f"{E.correct} Backup do servidor criado",
            CORES["info"],
            [
                ("Nome", (nome or "").strip() or "sem nome"),
                ("Arquivo", caminho.name),
                ("Canais", len(dados["canais"])),
                ("Cargos", len(dados["cargos"])),
                ("Por", f"{autor} ({autor.id})"),
            ],
        )
        return f"{E.correct} Backup criado: {_rotulo(dados)}"

    def _pedido(self, acao: str, arq) -> tuple[str, discord.ui.View]:
        """Texto e confirmação para restaurar ou apagar um backup escolhido."""
        rotulo = _rotulo(next(d for a, d in backup.listar() if a == arq))
        if acao == "apagar":
            return (
                f"{E.alert} Apagar o backup **{rotulo}**?\nIsso não dá pra desfazer. "
                "Os outros backups continuam."
            ), _confirmacao_apagar(arq.name)
        return (
            f"{E.alert} Restaurar **{rotulo}**?\nSubstitui o estado atual: apaga canais, cargos, emojis e "
            "outros recursos extras; permissões e configurações voltam ao snapshot. Vitrines são refeitas "
            "quando necessário. O histórico comum requer outra confirmação no canal de logs."
        ), _confirmacao_restaurar(arq.name)

    @app_commands.command(name="backup", description="[ dono ]")
    @app_commands.describe(
        acao="Criar um backup, restaurar um ou apagar um",
        nome="Nome para o novo backup (só para criar)",
        arquivo="Backup escolhido (para restaurar ou apagar)",
    )
    @app_commands.choices(
        acao=[
            app_commands.Choice(name="criar", value="criar"),
            app_commands.Choice(name="restaurar", value="restaurar"),
            app_commands.Choice(name="apagar", value="apagar"),
        ]
    )
    @app_commands.autocomplete(arquivo=_autocomplete_backup)
    async def backup_cmd(
        self,
        interaction: discord.Interaction,
        acao: str,
        nome: str = "",
        arquivo: str = "",
    ) -> None:
        if not _dono(interaction.user.id):
            await interaction.response.send_message(
                f"{E.wrong} Só o dono pode usar este comando.", ephemeral=True
            )
            return
        if acao in ("restaurar", "apagar"):
            arq = backup.caminho_do(arquivo)
            if arq is None:
                await interaction.response.send_message(
                    f"{E.wrong} Escolha um backup da lista.", ephemeral=True
                )
                return
            texto, view = self._pedido(acao, arq)
            await interaction.response.send_message(texto, view=view, ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        texto = await self._criar(interaction.guild, interaction.user, nome)
        await interaction.edit_original_response(content=texto)

    @commands.command(name="backup")
    async def backup_prefixo(self, ctx: commands.Context, *, texto: str = "") -> None:
        if not _dono(ctx.author.id):
            return
        primeira, _, resto = texto.strip().partition(" ")
        if primeira.lower() in ("restaurar", "apagar"):
            achados = _achados(resto.strip()) if resto.strip() else []
            if len(achados) != 1:
                await ctx.reply(
                    f"{E.alert} Use `>backup {primeira.lower()} <nome ou data>` com um termo que "
                    "aponte para um só backup.",
                    mention_author=False,
                )
                return
            pedido, view = self._pedido(primeira.lower(), achados[0][0])
            await ctx.reply(pedido, view=view, mention_author=False)
            return
        await ctx.reply(
            await self._criar(ctx.guild, ctx.author, texto), mention_author=False
        )

    @commands.command(name="restauracao")
    async def restauracao_prefixo(self, ctx: commands.Context, *, acao: str = "estado") -> None:
        if not _dono(ctx.author.id):
            return
        acao = acao.strip().casefold()
        if acao == "parar":
            texto = await backup.parar()
        elif acao == "continuar":
            texto = await backup.continuar(ctx.guild)
        elif acao in {"estado", "status"}:
            texto = backup.resumo_restauracao()
        else:
            texto = "Use `>restauracao parar`, `>restauracao continuar` ou `>restauracao estado`."
        await ctx.reply(texto, mention_author=False)

    # ── Botões de confirmação ────────────────────────────────────────────────

    @route(prefix="bkp:")
    async def _confirmar(self, interaction: discord.Interaction, cid: str) -> None:
        if not _dono(interaction.user.id):
            await interaction.response.send_message(
                f"{E.wrong} Só o dono pode mexer em backup.", ephemeral=True
            )
            return
        if cid == "bkp:no":
            await interaction.response.defer(ephemeral=True)
            await self._apagar_confirmacao_clicada(interaction)
            await interaction.followup.send(f"{E.wrong} Ação cancelada.", ephemeral=True)
            return
        partes = cid.split(":", 2)
        if len(partes) != 3 or partes[1] not in ("ok", "apagar"):
            await interaction.response.send_message(
                f"{E.wrong} Ação desconhecida.", ephemeral=True
            )
            return
        if partes[1] == "apagar":
            await interaction.response.defer(ephemeral=True)
            await self._apagar_confirmacao_clicada(interaction)
            nome_arq = partes[2]
            dados = next((d for a, d in backup.listar() if a.name == nome_arq), None)
            if dados is None or not backup.apagar(nome_arq):
                await interaction.followup.send(f"{E.wrong} Esse backup não existe mais.", ephemeral=True)
                return
            rotulo = _rotulo(dados)
            await notify.registrar(
                "backup_apagado",
                f"{E.delete} Backup apagado",
                CORES["aviso"],
                [
                    ("Backup", rotulo),
                    ("Por", f"{interaction.user} ({interaction.user.id})"),
                ],
            )
            await interaction.followup.send(f"{E.correct} Backup apagado: {rotulo}", ephemeral=True)
            return
        arq = backup.caminho_do(partes[2])
        if arq is None or interaction.guild is None:
            await interaction.response.edit_message(
                content=f"{E.wrong} Esse backup não existe mais.", view=None
            )
            return
        await interaction.response.defer(ephemeral=True)
        await self._apagar_confirmacao_clicada(interaction)
        dados = next(d for a, d in backup.listar() if a == arq)
        resultado = await backup.restaurar(
            interaction.guild,
            dados=dados,
            motivo=f"backup escolhido por {interaction.user} ({interaction.user.id})",
            arquivo=arq.name,
            confirmar=True,
            canal_confirmacao=interaction.channel_id,
        )
        if "erro" in resultado:
            texto = f"{E.wrong} {resultado['erro']}"
        else:
            texto = (
                f"{E.reload} Restauração iniciada. Estrutura e vitrines primeiro; "
                "vou pedir sua confirmação no canal de logs ou, se ele não estiver disponível, "
                "no canal em que você iniciou o comando. Confira `/backup` → estado da restauração para acompanhar."
            )
        await interaction.followup.send(texto, ephemeral=True)

    # ── Remover timeout escolhendo o membro (dono ou administrador) ──────────

    @app_commands.command(name="untimeout", description="[ dono/admin ] Remove o timeout de um membro")
    @app_commands.describe(
        membro="Quem perde o timeout",
        devolver_cargos="Devolver os cargos que o bot tirou na punição (padrão: não)",
    )
    async def untimeout(
        self, interaction: discord.Interaction, membro: discord.Member, devolver_cargos: bool = False
    ) -> None:
        if not _pode_untimeout(interaction.user):
            await interaction.response.send_message(f"{E.wrong} Só o dono ou administradores usam isso.", ephemeral=True)
            return
        if interaction.guild is None:
            await interaction.response.send_message(f"{E.wrong} Só funciona dentro do servidor.", ephemeral=True)
            return
        texto = await raid.remover_timeout(
            interaction.guild, membro, str(interaction.user), devolver_cargos=devolver_cargos
        )
        await interaction.response.send_message(texto, ephemeral=True)

    @commands.command(name="untimeout")
    async def untimeout_prefixo(self, ctx: commands.Context, membro: discord.Member, devolver: str = "") -> None:
        # >untimeout @membro            -> remove o timeout, não devolve cargos
        # >untimeout @membro devolver   -> remove o timeout e devolve os cargos
        if ctx.guild is None or not _pode_untimeout(ctx.author):
            return
        quer_devolver = devolver.lower() in ("devolver", "sim", "true", "1")
        await ctx.reply(
            await raid.remover_timeout(ctx.guild, membro, str(ctx.author), devolver_cargos=quer_devolver),
            mention_author=False,
        )

    # ── Revisão de palavrão suspeito (só o dono) ─────────────────────────────

    @route(prefix="pal:")
    async def _revisao_palavrao(self, interaction: discord.Interaction, cid: str) -> None:
        if not _dono(interaction.user.id):
            await interaction.response.send_message(f"{E.wrong} Só o dono decide isso.", ephemeral=True)
            return
        _, acao, rid = cid.split(":", 2)
        dados = json.loads(store.get_config(f"revisao:{rid}") or "null")
        if dados is None:
            await interaction.response.edit_message(content="Essa revisão já foi resolvida.", view=None)
            return
        if acao in ("baixo", "medio", "alto") and interaction.guild is not None:
            texto = await raid.punir_revisado(interaction.guild, dados, acao)
        elif acao == "nao":
            palavroes.adicionar_permitida(raid._PALAVROES_ARQ, dados["trecho"])
            texto = f"Marcado como não palavrão: `{dados['trecho']}` (não pergunto mais)."
        else:
            texto = "Ignorado."
        store.set_config(f"revisao:{rid}", "null")
        await interaction.response.edit_message(content=f"{texto} (por {interaction.user})", view=None)

    # ── Confirmação das mensagens de membros (canal de logs) ──────────────────

    @route(prefix="rst:")
    async def _confirmar_membros(self, interaction: discord.Interaction, cid: str) -> None:
        if not _dono(interaction.user.id):
            await interaction.response.send_message(
                f"{E.wrong} Só o dono confirma a restauração de mensagens.", ephemeral=True
            )
            return
        if interaction.guild is None:
            await interaction.response.send_message(f"{E.wrong} Só funciona dentro do servidor.", ephemeral=True)
            return
        sim = cid == "rst:sim"
        await interaction.response.defer(ephemeral=True)
        await self._apagar_confirmacao_clicada(interaction)
        texto = await backup.responder_confirmacao(interaction.guild, sim)
        await interaction.followup.send(
            f"{E.correct if sim else E.wrong} {texto} (por {interaction.user})",
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Backups(bot))
