"""Instância do bot: carregamento das cogs, roteamento de interações e sequência de boot."""

import asyncio
import logging
import time

import discord
from discord.ext import commands

from ..database.connection import close as close_db, init_db
from ..config import settings
from ..core import diario
from ..ui import emojis
from ..data import stock, store
from .secoes import cargos
from ..services import (
    antinuke,
    avisos,
    backup,
    context,
    mercadopago,
    papeis,
    payments,
    rest,
    scheduler,
    shelves,
    incidente,
)
from .router import Router


class ScopedTree(discord.app_commands.CommandTree):
    async def interaction_check(self, interaction):
        if interaction.guild_id not in (None, settings.GUILD_ID):
            await interaction.response.send_message(
                "Use no servidor configurado.", ephemeral=True
            )
            return False
        if (
            interaction.guild
            and incidente.ativo(interaction.guild.id)
            and not incidente.dono(interaction.guild, interaction.user.id)
        ):
            await interaction.response.send_message(
                "Servidor em contenção de raid.", ephemeral=True
            )
            return False
        return True


log = logging.getLogger("bot")
NIVEIS_DESCRICAO = ("[ admin ]", "[ dono ]")


def _registrar_erro_asyncio(loop, contexto: dict) -> None:
    """Torna falhas de tasks/futures sem await visíveis no mesmo log do bot."""
    erro = contexto.get("exception")
    tarefa = contexto.get("task") or contexto.get("future")
    nome_tarefa = ""
    if tarefa is not None:
        try:
            nome_tarefa = f" (tarefa {tarefa.get_name()})"
        except (AttributeError, RuntimeError):
            pass
    mensagem = contexto.get("message", "erro assíncrono sem descrição")
    if erro is not None:
        log.error(
            "exceção assíncrona não tratada%s: %s",
            nome_tarefa,
            mensagem,
            exc_info=(type(erro), erro, erro.__traceback__),
        )
    else:
        log.error("erro assíncrono sem exceção%s: %s", nome_tarefa, mensagem)

COGS = (
    "ghost.bot.cogs.gerenciar",
    "ghost.bot.cogs.comandos",
    "ghost.bot.cogs.comandos_anuncio",
    "ghost.bot.cogs.moderacao",
    "ghost.bot.cogs.ajuda",
    "ghost.bot.cogs.store",
    "ghost.bot.cogs.tickets",
    "ghost.bot.cogs.moderation",
    "ghost.bot.cogs.auditoria",
    "ghost.bot.cogs.agente",
    "ghost.bot.cogs.backup_cmds",
)
COMPONENT_TYPES = (
    discord.InteractionType.component,
    discord.InteractionType.modal_submit,
)


async def _nada(fn=None, *args):
    """Corrotina vazia ou que executa uma função síncrona (para entrar no gather)."""
    if fn is not None:
        return fn(*args)


async def _em_paralelo(*tarefas) -> None:
    nomes, corrotinas = zip(*tarefas)
    resultados = await asyncio.gather(*corrotinas, return_exceptions=True)
    for nome, resultado in zip(nomes, resultados):
        if isinstance(resultado, BaseException):
            log.error("falha na inicialização (%s)", nome, exc_info=resultado)


def _liberar_reservas() -> None:
    liberados = stock.release_stale()
    if liberados:
        log.info("reservas antigas liberadas: %d", liberados)


class GhostBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.members = True
        intents.message_content = True
        super().__init__(
            command_prefix=settings.PREFIX,
            intents=intents,
            help_command=None,
            tree_cls=ScopedTree,
            allowed_mentions=discord.AllowedMentions(
                everyone=False, roles=False, users=True, replied_user=False
            ),
        )
        self.router = Router()
        self._booted = False

    async def setup_hook(self) -> None:
        context.set_bot(self)
        asyncio.get_running_loop().set_exception_handler(_registrar_erro_asyncio)
        init_db()
        for erro in store.recarregar_catalogo():
            log.warning("produto do banco ignorado: %s", erro)
        for extension in COGS:
            await self.load_extension(extension)
        self._normalizar_descricoes_comandos()
        self._register_routes()

    def _normalizar_descricoes_comandos(self) -> None:
        """Deixa a descrição pública dos comandos apenas como nível de permissão."""
        corrigidos = 0
        for comando in self.tree.walk_commands():
            atual = str(getattr(comando, "description", "") or "").lower()
            nivel = "[ dono ]" if "[ dono ]" in atual else "[ admin ]"
            if getattr(comando, "description", None) != nivel:
                comando.description = nivel
                corrigidos += 1
        if corrigidos:
            log.info("descrições de comandos normalizadas: %d", corrigidos)

    def _register_routes(self) -> None:
        for cog in self.cogs.values():
            for name in dir(cog):
                handler = getattr(cog, name, None)
                for exact, prefix in getattr(handler, "__routes__", ()):
                    self.router.add(exact, prefix, handler)

    async def on_ready(self) -> None:
        log.info("logado como %s (%s)", self.user, self.user.id)
        if self._booted:
            return
        self._booted = True
        inicio = time.monotonic()
        guild = self.get_guild(settings.GUILD_ID)

        if guild is not None:
            await backup.retomar_pendente(guild)
            await incidente.retomar(guild)
            from ..services import inspecao_offline
            inspecao_offline.iniciar(guild)
        recuperando = bool(guild and incidente.ativo(guild.id)) or backup.estado_restauracao().get("status") == "rodando"
        # Fase 1: nada aqui depende de outra coisa, então roda junto.
        await _em_paralelo(
            ("comandos", self._sync_commands()),
            ("emojis", emojis.sync(guild) if guild is not None and not recuperando else _nada()),
            ("snapshot de canais", _nada(antinuke.snapshot_channels, guild) if not recuperando else _nada()),
            ("reservas antigas", _nada(_liberar_reservas)),
            ("diário do catálogo", _nada(diario.snapshot_catalogo)),
        )
        scheduler.resume()
        payments.resume()
        if settings.GOOGLE_DRIVE_ENABLED:
            from ..services import google_drive

            # Retoma uploads interrompidos antes do próximo backup agendado.
            google_drive.agendar_sincronizacao()

        if recuperando:
            backup.iniciar()
            return

        # Fase 2: vitrines usam os emojis já sincronizados; avisos é independente.
        await _em_paralelo(
            ("vitrines", shelves.republicar_todas()),
            ("novidades", avisos.anunciar_novidades()),
        )
        log.info("bot pronto em %.1fs", time.monotonic() - inicio)
        backup.iniciar()
        if guild is not None:
            await _em_paralelo(
                ("cargos", cargos.migrar(guild)),
                ("membros", papeis.dar_membro_a_todos(guild)),
            )

    async def _sync_commands(self) -> None:
        self._normalizar_descricoes_comandos()
        if settings.GUILD_ID:
            guild = discord.Object(id=settings.GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
        else:
            synced = await self.tree.sync()
        log.info("comandos sincronizados: %d", len(synced))

    async def on_interaction(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id not in (None, settings.GUILD_ID):
            return
        if (
            interaction.guild
            and incidente.ativo(interaction.guild.id)
            and not incidente.dono(interaction.guild, interaction.user.id)
        ):
            await interaction.response.send_message(
                "Servidor em contenção de raid.", ephemeral=True
            )
            return
        if interaction.type not in COMPONENT_TYPES:
            return
        if await self.router.dispatch(interaction):
            return
        cid = (interaction.data or {}).get("custom_id", "")
        log.warning(
            "componente sem handler: %s (canal %s)", cid, interaction.channel_id
        )
        if (
            interaction.type == discord.InteractionType.component
            and not interaction.response.is_done()
        ):
            await interaction.response.defer()

    async def on_message(self, message: discord.Message) -> None:
        if message.guild is not None and message.guild.id != settings.GUILD_ID:
            return
        moderation = self.get_cog("Moderation")
        if moderation is not None and await moderation.screen(message):
            return
        if message.guild is not None and not message.author.bot:
            total = cargos.registrar_mensagem(message.author.id)
            if float(total) in {float(n["valor"]) for n in cargos.niveis("mensagens")}:
                await cargos.sincronizar(message.guild, message.author.id)
        await self.process_commands(message)

    async def close(self) -> None:
        from ..agente import loop

        for cid in tuple(loop.SESSOES):
            loop.fechar(cid)
        await payments.close()
        await scheduler.close()
        await incidente.close()
        from ..services import inspecao_offline
        await inspecao_offline.fechar()
        await backup.close()
        await shelves.close()
        await mercadopago.close()
        await rest.close()
        await super().close()
        close_db()
