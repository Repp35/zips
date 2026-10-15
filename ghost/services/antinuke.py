"""Proteção anti-nuke: snapshot de canais, restauração de canais apagados e lockdown do servidor."""

import asyncio
import logging
import json
import time
from collections import defaultdict, deque

import discord

from ..config import settings
from ..data import store
from ..ui.emojis import E
from ..config.settings import CORES
from . import backup, context, raid, incidente

log = logging.getLogger("antinuke")

_deletes: dict[int, deque[float]] = defaultdict(deque)
_creates: dict[int, deque[float]] = defaultdict(deque)
_nomes_repetidos: dict[tuple[str, str], deque[float]] = defaultdict(deque)
_lockdown_ativo = False


def ligado() -> bool:
    """Anti-nuke: restauração de canais apagados em massa e lockdown. Desligado, só o anti-raid por autor segue."""
    return raid.flag("antinuke_ativo")


def snapshot_channels(guild: discord.Guild | None) -> None:
    if guild is None or incidente.ativo(guild.id):
        return
    # No boot, um canal ausente pode ter sido excluído durante a desconexão.
    # Só a reconciliação com backup confiável decide se o snapshot é obsoleto.
    linhas = [linha for linha in map(_linha, guild.channels) if linha is not None]
    store.save_channel_snapshots(linhas)
    log.info("snapshot de %d canais salvo", len(linhas))


def _tipo(channel: discord.abc.GuildChannel) -> str | None:
    if isinstance(channel, discord.TextChannel):
        return "text"
    if isinstance(channel, discord.VoiceChannel):
        return "voice"
    if isinstance(channel, discord.CategoryChannel):
        return "category"
    return None


def _linha(channel: discord.abc.GuildChannel) -> tuple | None:
    tipo = _tipo(channel)
    if tipo is None:
        return None
    return (
        str(channel.id),
        channel.name,
        tipo,
        str(channel.category_id) if channel.category_id else None,
        getattr(channel, "topic", None),
        channel.position,
        json.dumps(backup._overwrites(channel)),
    )


def _save(channel: discord.abc.GuildChannel) -> None:
    linha = _linha(channel)
    if linha is not None:
        store.save_channel_snapshots([linha])


def _count(janelas: dict[int, deque[float]], autor_id: int, limite: int) -> bool:
    """Conta por executor para não transformar uma ação legítima da equipe em ataque global."""
    agora = time.monotonic()
    janela = janelas[autor_id]
    while janela and agora - janela[0] > settings.NUKE_JANELA_S:
        janela.popleft()
    janela.append(agora)
    if len(janela) >= limite:
        janela.clear()
        return True
    return False


def _nome_repetido(channel: discord.abc.GuildChannel) -> bool:
    chave = (
        type(channel).__name__,
        channel.name.casefold(),
    )
    agora = time.monotonic()
    fila = _nomes_repetidos[chave]
    while fila and agora - fila[0] > settings.NUKE_JANELA_S:
        fila.popleft()
    fila.append(agora)
    return len(fila) >= settings.RAID_CANAIS_DUPLICADOS_MIN


async def _apagar_canal(channel: discord.abc.GuildChannel) -> None:
    try:
        await channel.delete(reason="Anti-raid: criação em massa")
    except discord.NotFound:
        return
    except discord.HTTPException as exc:
        log.error("anti-nuke não conseguiu apagar o canal %s (%s): %s", channel.id, channel.name, exc)


_bloqueio_ate = 0.0
_acompanhando = False


def _bloqueio_ativo() -> bool:
    return time.monotonic() < _bloqueio_ate


async def _acompanhar_bloqueio(guild: discord.Guild) -> None:
    """Quando ninguém cria canal pelo tempo do bloqueio, o bloqueio termina e isso vai para o log."""
    global _acompanhando
    try:
        while (restante := _bloqueio_ate - time.monotonic()) > 0:
            await asyncio.sleep(restante)
        await raid.log_acesso(
            f"{E.correct} Bloqueio de criação encerrado",
            CORES["info"],
            [
                ("Motivo", f"ninguém criou canal por {settings.NUKE_BLOQUEIO_S} s"),
                ("Servidor", guild.name),
            ],
        )
    finally:
        _acompanhando = False


async def _iniciar_bloqueio(guild: discord.Guild, motivo: str) -> None:
    """Cada canal criado durante o bloqueio renova o prazo. Sem criação por NUKE_BLOQUEIO_S, ele termina."""
    global _bloqueio_ate, _acompanhando
    if not _bloqueio_ativo():
        await raid.log_acesso(
            f"{E.alert} Bloqueio de criação ativado",
            CORES["erro"],
            [
                ("Motivo", motivo),
                ("Servidor", guild.name),
                ("Efeito", "todo canal novo é apagado até a criação parar"),
            ],
        )
    _bloqueio_ate = time.monotonic() + settings.NUKE_BLOQUEIO_S
    if not _acompanhando:
        _acompanhando = True
        asyncio.create_task(_acompanhar_bloqueio(guild))


async def on_channel_created(channel: discord.abc.GuildChannel) -> None:
    guild = channel.guild
    if not (ligado() or raid.ligado() or incidente.ativo(guild.id)):
        _save(channel)
        return
    autor = await raid.autor_recente(
        guild, discord.AuditLogAction.channel_create, channel.id
    )
    if autor is not None and (autor.id == raid._bot_id() or incidente.dono(guild, autor.id)):
        if autor.id == raid._bot_id() and incidente.ativo(guild.id):
            return
        if not incidente.ativo(guild.id):
            _save(channel)
        return
    atencao = (
        await raid.conter_acao_em_atencao(guild, autor, "criação de canal")
        if autor is not None else False
    )
    if autor is None and backup.restaurando():
        # Auditoria pode chegar depois do gateway; a reconciliação fará a limpeza final.
        return
    if autor is None:
        if incidente.ativo(guild.id):
            await _apagar_canal(channel)
            return
        if _nome_repetido(channel):
            await incidente.iniciar(guild, "Criação rápida de canais com nomes repetidos")
            await _apagar_canal(channel)
            return
        # Não incorpore uma criação sem autoria ao snapshot usado como referência.
        log.warning("canal %s criado sem executor confirmado; não entrou no snapshot anti-nuke", channel.id)
        return
    raid_atingiu = raid.excedeu("channel_create", autor)
    nuke_atingiu = ligado() and _count(_creates, autor.id, settings.NUKE_CREATE_LIMITE)
    nomes_repetidos = _nome_repetido(channel)
    atingiu = atencao or raid_atingiu or nuke_atingiu or nomes_repetidos
    if atingiu or incidente.ativo(guild.id):
        await incidente.iniciar(guild, "Criação de canais durante raid")
        if autor and not atencao and not incidente.dono(guild, autor.id):
            await raid.punir(
                guild, autor.id,
                "Criação suspeita de canais em massa",
                tirar_cargos=True
            )
        await _apagar_canal(channel)
        return
    _save(channel)


async def on_channel_deleted(channel: discord.abc.GuildChannel) -> None:
    guild = channel.guild
    if not (ligado() or raid.ligado() or incidente.ativo(guild.id)):
        store.pop_channel_snapshot(channel.id)
        return
    autor = await raid.autor_recente(
        guild, discord.AuditLogAction.channel_delete, channel.id
    )
    if autor is not None and (autor.id == raid._bot_id() or incidente.dono(guild, autor.id)):
        return
    atencao = (
        await raid.conter_acao_em_atencao(guild, autor, "exclusão de canal")
        if autor is not None else False
    )
    if autor is None and backup.restaurando():
        return
    raid_atingiu = raid.excedeu("channel_delete", autor)
    nuke_atingiu = bool(autor and ligado() and _count(_deletes, autor.id, settings.NUKE_DELETE_LIMITE))
    atingiu = atencao or raid_atingiu or nuke_atingiu
    if atingiu or incidente.ativo(guild.id):
        await incidente.iniciar(guild, "Exclusão de canais durante raid")
        if autor and not atencao and not incidente.dono(guild, autor.id):
            await raid.punir(
                guild, autor.id, "Exclusão de canais em massa", tirar_cargos=True
            )
        # Só repõe canais do backup fixado ANTES do ataque, nunca um snapshot contaminado.
        s = incidente.estado(guild.id)
        if s.get("backup"):
            dados = backup._carregar_backup(s["backup"])
            original = next(
                (
                    c
                    for c in dados.get("canais", [])
                    if int(s.get("mapa", {}).get(c["id"], c["id"])) == channel.id
                ),
                None,
            )
            if original:
                await _repor_backup(guild, original, s)


async def _repor_backup(guild, original, estado):
    # A rotina compartilhada suporta também fórum e palco. Sem limpeza de extras aqui.
    dados = {"schema_version": 1, "canais": [original]}
    mapa = dict(estado.get("mapa", {}))
    await backup._canais(guild, dados, mapa)
    estado["mapa"] = mapa
    incidente.salvar(guild.id)


async def _restore(guild: discord.Guild, snap) -> None:
    category = (
        guild.get_channel(int(snap["category_id"])) if snap["category_id"] else None
    )
    try:
        if not snap["permissoes"]:
            log.warning(
                "snapshot legado sem permissões; use o backup completo para %s",
                snap["name"],
            )
            return
        overwrites = backup._permissoes(guild, {}, json.loads(snap["permissoes"]))
        if snap["type"] == "text":
            await guild.create_text_channel(
                name=snap["name"],
                category=category,
                topic=snap["topic"],
                position=snap["position"] or 0,
                overwrites=overwrites,
            )
        elif snap["type"] == "category":
            await guild.create_category(
                name=snap["name"], position=snap["position"] or 0, overwrites=overwrites
            )
        else:
            await guild.create_voice_channel(
                name=snap["name"],
                category=category,
                position=snap["position"] or 0,
                overwrites=overwrites,
            )
        await raid.log_acesso(
            f"{E.correct} Canal restaurado", CORES["info"], [("Canal", snap["name"])]
        )
    except (discord.HTTPException, ValueError) as exc:
        log.warning("falha ao restaurar #%s: %s", snap["name"], exc)
        await raid.log_acesso(
            f"{E.alert} Falha ao restaurar canal",
            CORES["erro"],
            [("Canal", snap["name"]), ("Erro", str(exc)[:200])],
        )


async def _aplicar_everyone(guild: discord.Guild, travar: bool) -> None:
    everyone = guild.default_role
    limite = asyncio.Semaphore(5)

    async def um(channel: discord.abc.GuildChannel) -> None:
        async with limite:
            try:
                overwrite = channel.overwrites_for(everyone)
                key = f"nuke_lock:{guild.id}:{channel.id}"
                if travar:
                    if store.get_config(key) is None:
                        allow, deny = overwrite.pair()
                        store.set_config(key, json.dumps([allow.value, deny.value]))
                    overwrite.send_messages = False
                    overwrite.connect = False
                else:
                    saved = store.get_config(key)
                    if saved is None:
                        return
                    allow, deny = json.loads(saved)
                    overwrite = discord.PermissionOverwrite.from_pair(
                        discord.Permissions(allow), discord.Permissions(deny)
                    )
                await channel.set_permissions(
                    everyone, overwrite=None if overwrite.is_empty() else overwrite
                )
                if not travar:
                    store.reset_config(key)
            except discord.HTTPException as exc:
                log.warning("anti-nuke não conseguiu ajustar permissões de %s: %s", channel.id, exc)

    canais = [*guild.text_channels, *guild.voice_channels]
    await asyncio.gather(*(um(c) for c in canais))


async def lockdown(guild: discord.Guild, motivo: str) -> None:
    global _lockdown_ativo
    if _lockdown_ativo:
        return
    _lockdown_ativo = True
    await _aplicar_everyone(guild, True)
    try:
        dono = await context.user(settings.ADMIN_ID)
        await dono.send(
            f"{E.alert} **ANTI-NUKE ATIVADO**\nMotivo: `{motivo}`\nServer: `{guild.name}`\n\n"
            f"Use `>reverter` no privado para desfazer o lockdown."
        )
    except discord.HTTPException as exc:
        log.error("anti-nuke não conseguiu enviar o aviso ao dono: %s", exc)


async def revert(guild: discord.Guild) -> None:
    global _lockdown_ativo
    await _aplicar_everyone(guild, False)
    _lockdown_ativo = False


def lockdown_active() -> bool:
    return _lockdown_ativo or any(
        k.startswith("nuke_lock:") for k in store.todas_configs()
    )
