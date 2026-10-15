"""Backup e restauração do servidor.

- Um único manifesto diário (`backup-diario.json`), atualizado incrementalmente. Backups manuais ficam em arquivos separados; a retenção mantém MANTER manuais.
- Restauração: recria o que foi apagado, ajusta o que foi alterado (nome, descrição, ícone, canais, cargos)
  e republica as vitrines. Em raid, roda sozinha depois da detecção. Também pode ser pedida no /gerenciar.
- IDs salvos só remapeiam referências; canais e cargos são reconciliados por tipo, conteúdo e estrutura.
- Também guarda threads (ativas e arquivadas) com os membros de cada uma, e os cargos de cada membro humano.
  Mensagens acessíveis das threads são guardadas pelo snapshot de `mensagens.py`; membros que saíram não são guardados.
- Mensagens: o arquivo diário em services/mensagens.py guarda o que foi escrito; a restauração reenvia nos canais recriados.
- Backups schema 2 também sincronizam emojis, cargos/canais extras e configurações gerais.
- O Discord não permite restaurar IDs, histórico completo de mensagens, membros que saíram,
  itens gerenciados por integrações ou algumas configurações exclusivas do dono.
"""

import asyncio
import base64
import io
import hashlib
import json
import logging
import os
import re
import time
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import discord

from .. import catalogo
from ..config import settings
from ..core.files import atomic_text
from ..data import store
from ..ui.emojis import E
from ..config.settings import CORES
from . import context, envs, notify, regras, rest, reconciliar

log = logging.getLogger("backup")

PASTA: Path = settings.BASE_DIR / "backups"
MANTER = settings.BACKUP_MANTER
NOME_BACKUP_DIARIO = "backup-diario.json"
RAID_JANELA_S = settings.BACKUP_RAID_JANELA_S
DEBOUNCE_S = settings.BACKUP_DEBOUNCE_S
INTERVALO_S = settings.BACKUP_INTERVALO_S
_raid_ate = 0.0
_restauracao_agendada = False
_ocupado = False
_trava_backup = asyncio.Lock()
_cache_inspecao: tuple[str, int, int, dict] | None = None
_cache_threads_restore: tuple[int, float, dict[str, discord.Thread], bool] | None = None
_trava_membros_threads = asyncio.Lock()
_ultima_adicao_membro_thread = 0.0
INTERVALO_ADICAO_MEMBRO_THREAD_S = 3.1
_trava_exclusao_canal = asyncio.Lock()
_ultima_exclusao_canal = 0.0
INTERVALO_EXCLUSAO_CANAL_S = 1.1


def restaurando() -> bool:
    return _ocupado


async def _excluir_canal_limitado(canal, motivo: str) -> None:
    """Serializa exclusões de canais e deixa espaço entre chamadas da mesma rota."""
    global _ultima_exclusao_canal
    async with _trava_exclusao_canal:
        espera = INTERVALO_EXCLUSAO_CANAL_S - (time.monotonic() - _ultima_exclusao_canal)
        if espera > 0:
            await asyncio.sleep(espera)
        try:
            await canal.delete(reason=motivo)
        finally:
            _ultima_exclusao_canal = time.monotonic()
# Pausa entre operações de canal: evita rajadas em PATCH /guilds/{id}/channels e PATCH /channels/{id}.
PAUSA_ENTRE_CANAIS_S = settings.BACKUP_PAUSA_ENTRE_CANAIS_S
_trava_canais = asyncio.Lock()
_SEM_VALOR = object()


# ── Arquivos ──────────────────────────────────────────────────────────────────


def arquivos() -> list[Path]:
    if not PASTA.exists():
        return []
    return sorted(
        PASTA.glob("backup-*.json"),
        key=lambda p: (p.name == NOME_BACKUP_DIARIO, p.name),
    )


def _motivo_manifesto(caminho: Path) -> str | None:
    """Lê só os metadados do manifesto; manifestos ilegíveis são preservados como manuais."""
    try:
        return json.loads(caminho.read_text(encoding="utf-8")).get("motivo")
    except (OSError, ValueError, AttributeError):
        return None


def _arquivos_manuais() -> list[Path]:
    return [
        p for p in arquivos()
        if p.name != NOME_BACKUP_DIARIO and _motivo_manifesto(p) != "diário"
    ]


def _arquivos_diarios_legados() -> list[Path]:
    return [
        p for p in arquivos()
        if p.name != NOME_BACKUP_DIARIO and _motivo_manifesto(p) == "diário"
    ]


def _arquivos_protegidos() -> set[str]:
    """Arquivos que uma restauração ativa ou retomável ainda pode precisar."""
    protegidos: set[str] = set()
    st = estado_restauracao()
    if st.get("status") in {"rodando", "aguardando", "erro", "parado"} and st.get("arquivo"):
        protegidos.add(st["arquivo"])
    try:
        from . import incidente

        nome = incidente.estado(settings.GUILD_ID).get("backup")
        if nome:
            protegidos.add(nome)
    except Exception:
        log.exception("não consegui verificar backups protegidos por incidente")
        # Falha fechada: uma limpeza não deve remover snapshots que talvez estejam em uso.
        return {p.name for p in arquivos()}
    return protegidos


def _materializar(documento: dict) -> dict:
    """Reconstrói snapshot por objetos imutáveis; backups v4 antigos seguem legíveis."""
    if not documento.get("objetos"):
        return documento

    def ler_objeto(digest: str) -> bytes:
        if not isinstance(digest, str) or re.fullmatch(r"[a-f0-9]{64}", digest) is None:
            raise ValueError("referência de objeto inválida no backup")
        bruto = (PASTA / "objetos" / f"{digest}.json").read_bytes()
        if hashlib.sha256(bruto).hexdigest() != digest:
            raise ValueError("objeto de backup corrompido")
        return bruto

    resultado = {k: v for k, v in documento.items() if k != "objetos"}
    for chave, digest in documento["objetos"].items():
        bruto = ler_objeto(digest)
        resultado[chave] = json.loads(bruto)
    msg = resultado.get("mensagens")
    if msg and isinstance(msg.get("canais"), dict):
        for cid, ref in list(msg["canais"].items()):
            if isinstance(ref, dict) and "objeto" in ref:
                digest = ref["objeto"]
                bruto = ler_objeto(digest)
                msg["canais"][cid] = json.loads(bruto)
    return resultado


def _ler_backup(caminho: Path) -> dict:
    return _materializar(json.loads(caminho.read_text(encoding="utf-8")))


def ultimo_valido() -> tuple[Path, dict] | None:
    for arq in reversed(arquivos()):
        try:
            dados = _ler_backup(arq)
            if not isinstance(dados, dict):
                continue
            schema = int(dados.get("schema_version", 1) or 1)
            if schema >= 2 and (
                isinstance(dados.get("canais"), list)
                and isinstance(dados.get("cargos"), list)
                and isinstance(dados.get("servidor"), dict)
            ):
                return arq, dados
        except (OSError, ValueError, TypeError, KeyError):
            log.exception("backup %s ilegível; usando a versão anterior", arq.name)
    return None


def ultimo() -> dict | None:
    encontrado = ultimo_valido()
    return encontrado[1] if encontrado else None


def ultimo_para_inspecao(guild_id: int) -> dict | None:
    """Snapshot estrutural confiável para detectar dano, mesmo sem histórico completo.

    A inspeção offline não deve desaparecer só porque o arquivo ainda não terminou
    de arquivar mensagens. A restauração automática continua usando `ultimo_valido`,
    que exige histórico completo; este método serve somente para comparar estrutura.
    """
    global _cache_inspecao
    for caminho in reversed(arquivos()):
        try:
            stat = caminho.stat()
            cache = _cache_inspecao
            if cache and cache[:3] == (str(caminho), stat.st_mtime_ns, stat.st_size):
                dados = cache[3]
            else:
                dados = _ler_backup(caminho)
            if (
                str(dados.get("guild_id")) == str(guild_id)
                and isinstance(dados.get("canais"), list)
                and isinstance(dados.get("cargos"), list)
                and isinstance(dados.get("servidor"), dict)
            ):
                _cache_inspecao = (str(caminho), stat.st_mtime_ns, stat.st_size, dados)
                return dados
        except (OSError, ValueError, KeyError, TypeError):
            log.exception("snapshot %s indisponível para inspeção offline", caminho.name)
    return None


def _gravar_incremental(caminho: Path, dados: dict) -> None:
    """Conteúdo imutável compartilhado entre versões; manifesto atômico é publicado por último."""
    objetos = PASTA / "objetos"
    objetos.mkdir(parents=True, exist_ok=True)
    manifesto = {k: v for k, v in dados.items() if k not in ("mensagens", "canais", "cargos", "threads", "servidor", "emojis", "webhooks", "convites", "banimentos", "figurinhas", "automod", "eventos_programados", "membros_cargos", "membros")}
    refs = {}
    for chave in ("mensagens", "canais", "cargos", "threads", "servidor", "emojis", "webhooks", "convites", "banimentos", "figurinhas", "automod", "eventos_programados", "membros_cargos", "membros"):
        valor = dados.get(chave)
        if chave == "mensagens":
            canais = valor.get("canais", {})
            refs_msg = {}
            for canal_id, registros in canais.items():
                bruto = json.dumps(registros, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
                digest = hashlib.sha256(bruto).hexdigest()
                arquivo = objetos / f"{digest}.json"
                if not arquivo.exists():
                    atomic_text(arquivo, bruto.decode())
                refs_msg[canal_id] = digest
            valor = {**valor, "canais": {cid: {"objeto": digest} for cid, digest in refs_msg.items()}}
        bruto = json.dumps(valor, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        digest = hashlib.sha256(bruto).hexdigest()
        arquivo = objetos / f"{digest}.json"
        if not arquivo.exists():
            atomic_text(arquivo, bruto.decode())
        refs[chave] = digest
    manifesto["objetos"] = refs
    atomic_text(caminho, json.dumps(manifesto, ensure_ascii=False))
    global _cache_inspecao
    _cache_inspecao = None



def _limpar() -> None:
    protegidos = _arquivos_protegidos()
    # Só remove arquivos automáticos legados depois que o manifesto fixo foi
    # publicado com sucesso. Uma falha durante a primeira migração preserva a base.
    if (PASTA / NOME_BACKUP_DIARIO).is_file():
        for antigo in _arquivos_diarios_legados():
            if antigo.name in protegidos:
                continue
            try:
                antigo.unlink()
            except OSError:
                log.warning("não consegui remover backup diário legado %s", antigo.name, exc_info=True)

    manuais = _arquivos_manuais()
    removidos = 0
    for antigo in manuais:
        if len(manuais) - removidos <= MANTER:
            break
        if antigo.name in protegidos:
            continue
        try:
            antigo.unlink()
            removidos += 1
        except OSError:
            log.warning("não consegui remover backup antigo %s", antigo.name, exc_info=True)

    # Deduplicação imutável gera objetos órfãos quando manifestos antigos saem
    # da retenção. Varre as referências sobreviventes antes de apagar qualquer
    # objeto; manifesto ilegível faz a limpeza falhar fechada.
    objetos = PASTA / "objetos"
    if not objetos.exists():
        return
    referenciados: set[str] = set()
    pendentes: list[str] = []

    def coletar(valor) -> None:
        if isinstance(valor, dict):
            digest = valor.get("objeto")
            if isinstance(digest, str) and re.fullmatch(r"[a-f0-9]{64}", digest):
                pendentes.append(digest)
            for filho in valor.values():
                coletar(filho)
        elif isinstance(valor, list):
            for filho in valor:
                coletar(filho)

    try:
        for manifesto in arquivos():
            documento = json.loads(manifesto.read_text(encoding="utf-8"))
            refs = documento.get("objetos", {})
            if not isinstance(refs, dict):
                return
            for digest in refs.values():
                if isinstance(digest, str) and re.fullmatch(r"[a-f0-9]{64}", digest):
                    pendentes.append(digest)
        while pendentes:
            digest = pendentes.pop()
            if digest in referenciados:
                continue
            referenciados.add(digest)
            caminho = objetos / f"{digest}.json"
            conteudo = json.loads(caminho.read_text(encoding="utf-8"))
            coletar(conteudo)
    except (OSError, ValueError, TypeError) as exc:
        log.warning("coleta de objetos órfãos adiada: %s", exc)
        return
    for caminho in objetos.glob("*.json"):
        if caminho.stem not in referenciados:
            try:
                caminho.unlink()
            except OSError:
                log.warning("objeto órfão não removido: %s", caminho.name, exc_info=True)


def marcar_raid() -> None:
    global _raid_ate
    _raid_ate = time.monotonic() + RAID_JANELA_S


def em_raid() -> bool:
    from . import incidente

    return incidente.ativo() or time.monotonic() < _raid_ate


# ── Coleta ────────────────────────────────────────────────────────────────────


def _overwrites(canal) -> list[dict]:
    saida = []
    for alvo, ow in canal.overwrites.items():
        allow, deny = ow.pair()
        saida.append(
            {
                "id": str(alvo.id),
                "tipo": "cargo" if isinstance(alvo, discord.Role) else "membro",
                "allow": allow.value,
                "deny": deny.value,
            }
        )
    return saida


def _canal_dados(canal) -> dict:
    if isinstance(canal, discord.CategoryChannel):
        tipo = "categoria"
    elif isinstance(canal, discord.StageChannel):
        tipo = "palco"
    elif isinstance(canal, discord.ForumChannel):
        tipo = "forum"
    elif isinstance(canal, discord.VoiceChannel):
        tipo = "voz"
    else:
        tipo = "texto"
    dados = {
        "id": str(canal.id),
        "tipo": tipo,
        "nome": canal.name,
        "posicao": canal.position,
        "pai": str(canal.category_id) if canal.category_id else None,
        "permissoes": _overwrites(canal),
    }
    if tipo == "texto":
        dados.update(
            topico=canal.topic,
            nsfw=canal.nsfw,
            lento=canal.slowmode_delay,
            arquivo_padrao=canal.default_auto_archive_duration,
        )
    elif tipo in ("voz", "palco"):
        dados.update(
            bitrate=canal.bitrate,
            limite=canal.user_limit,
            regiao=canal.rtc_region,
            qualidade=canal.video_quality_mode.value,
        )
        if tipo == "palco":
            dados["topico"] = getattr(canal, "topic", None)
    elif tipo == "forum":
        dados.update(
            topico=canal.topic,
            nsfw=canal.nsfw,
            lento=canal.slowmode_delay,
            arquivo_padrao=canal.default_auto_archive_duration,
            lento_threads=canal.default_thread_slowmode_delay,
            layout=canal.default_layout.value,
            ordenacao=canal.default_sort_order.value,
            tags=[_tag_dados(tag) for tag in canal.available_tags],
            reacao_padrao=(
                canal.default_reaction_emoji.name
                if canal.default_reaction_emoji
                else None
            ),
        )
    return dados


async def _cargo_dados(cargo: discord.Role) -> dict:
    icone = cargo.display_icon
    return {
        "id": str(cargo.id),
        "nome": cargo.name,
        "cor": cargo.colour.value,
        "permissoes": cargo.permissions.value,
        "destaque": cargo.hoist,
        "mencionavel": cargo.mentionable,
        "posicao": cargo.position,
        "icone": await _asset_dados(icone)
        if isinstance(icone, discord.Asset)
        else None,
        "emoji": icone if isinstance(icone, str) else None,
        "cor_secundaria": cargo.secondary_colour.value
        if cargo.secondary_colour
        else None,
        "cor_terciaria": cargo.tertiary_colour.value if cargo.tertiary_colour else None,
    }


async def _baixar(url: str) -> bytes:
    """Baixa pela URL. Emoji e figurinha expõem a URL como texto (não são Asset), então não têm .read()."""
    http = await rest._http()
    async with http.get(url) as resp:
        if resp.status >= 400:
            raise RuntimeError(f"HTTP {resp.status} ao baixar {url}")
        return await resp.read()


async def _asset_dados(asset) -> str | None:
    if asset is None:
        return None
    return base64.b64encode(await asset.read()).decode("ascii")


def _tag_dados(tag: discord.ForumTag) -> dict:
    emoji = tag.emoji
    return {
        "id": str(tag.id),
        "nome": tag.name,
        "emoji_id": str(emoji.id)
        if isinstance(emoji, discord.PartialEmoji) and emoji.id
        else None,
        "emoji_nome": emoji.name
        if isinstance(emoji, discord.PartialEmoji) and emoji.id
        else None,
        "emoji": str(emoji)
        if emoji and not (isinstance(emoji, discord.PartialEmoji) and emoji.id)
        else None,
        "moderada": tag.moderated,
    }


def _emoji_de_backup(item: dict, mapa: dict):
    if item.get("emoji_id"):
        old = item["emoji_id"]
        novo = mapa.get(f"emoji:{old}")
        if not novo:
            raise ValueError(f"emoji {old} não foi mapeado para um recurso atual")
        return discord.PartialEmoji(
            name=item.get("emoji_nome") or "emoji",
            id=int(novo),
        )
    return item.get("emoji")


def _tag_de_backup(item: dict, mapa: dict) -> discord.ForumTag:
    return discord.ForumTag(
        name=item["nome"],
        emoji=_emoji_de_backup(item, mapa),
        moderated=item.get("moderada", False),
    )


async def _webhooks_dados(guild: discord.Guild) -> list[dict]:
    saida = []
    for canal in _canais_do_servidor(guild):
        if not hasattr(canal, "webhooks"):
            continue
        for webhook in await canal.webhooks():
            if webhook.type != discord.WebhookType.incoming:
                continue  # webhooks de integrações/aplicações são geridos pelo próprio Discord
            saida.append(
                {
                    "id": str(webhook.id),
                    "canal": str(canal.id),
                    "nome": webhook.name,
                    "criador": str(webhook.user.id) if webhook.user else None,
                    "icone": await _asset_dados(webhook.avatar),
                }
            )
    return saida


async def _convites_dados(guild: discord.Guild) -> list[dict]:
    convites = await guild.invites()
    return [
        {
            "codigo": convite.code,
            "canal": str(convite.channel.id) if convite.channel else None,
            "criador": str(convite.inviter.id) if convite.inviter else None,
            "criado_em": convite.created_at.isoformat() if convite.created_at else None,
            "expira_em": convite.expires_at.isoformat() if convite.expires_at else None,
            "max_age": convite.max_age,
            "max_uses": convite.max_uses,
            "usos": convite.uses,
            "temporario": convite.temporary,
            "target_type": convite.target_type.value if convite.target_type else None,
            "target_user": str(convite.target_user.id) if convite.target_user else None,
            "target_application": convite.target_application.id
            if convite.target_application
            else None,
        }
        for convite in convites
        if convite.channel is not None
    ]


async def _banimentos_dados(guild: discord.Guild) -> list[dict]:
    return [
        {"user_id": str(entry.user.id), "motivo": entry.reason}
        async for entry in guild.bans(limit=None)
    ]


async def _figurinhas_dados(guild: discord.Guild) -> list[dict]:
    saida = []
    for sticker in await guild.fetch_stickers():
        if sticker.guild_id != guild.id or not sticker.available:
            continue
        saida.append(
            {
                "id": str(sticker.id),
                "nome": sticker.name,
                "descricao": sticker.description,
                "emoji": sticker.emoji,
                "formato": sticker.format.value,
                "imagem": base64.b64encode(await _baixar(sticker.url)).decode("ascii"),
            }
        )
    return saida


async def _automod_dados(guild: discord.Guild) -> list[dict]:
    saida = []
    for rule in await guild.fetch_automod_rules():
        trigger = rule.trigger
        saida.append(
            {
                "id": str(rule.id),
                "nome": rule.name,
                "evento": rule.event_type.value,
                "gatilho": {
                    "tipo": trigger.type.value,
                    "keywords": trigger.keyword_filter,
                    "presets": trigger.presets.value if trigger.presets else 0,
                    "allow_list": trigger.allow_list,
                    "mention_limit": trigger.mention_limit,
                    "regex": trigger.regex_patterns,
                    "mention_raid_protection": trigger.mention_raid_protection,
                },
                "acoes": [
                    {
                        "tipo": action.type.value,
                        "canal": action.channel_id,
                        "duracao": action.duration.total_seconds()
                        if action.duration
                        else None,
                        "mensagem": action.custom_message,
                    }
                    for action in rule.actions
                ],
                "habilitada": rule.enabled,
                "cargos_isentos": [str(x) for x in rule.exempt_role_ids],
                "canais_isentos": [str(x) for x in rule.exempt_channel_ids],
            }
        )
    return saida


async def _eventos_programados_dados(guild: discord.Guild) -> list[dict]:
    saida = []
    for event in await guild.fetch_scheduled_events(with_counts=False):
        saida.append(
            {
                "id": str(event.id),
                "nome": event.name,
                "descricao": event.description,
                "inicio": event.start_time.isoformat(),
                "fim": event.end_time.isoformat() if event.end_time else None,
                "privacidade": event.privacy_level.value,
                "tipo": event.entity_type.value,
                "canal": str(event.channel_id) if event.channel_id else None,
                "local": event.location,
                "estado": event.status.value,
                "imagem": await _asset_dados(event.cover_image),
            }
        )
    return saida


async def _emojis_dados(guild: discord.Guild) -> list[dict]:
    saida = []
    for emoji in guild.emojis:
        if emoji.managed:
            continue  # emoji de integração: a API não permite recriá-lo ou removê-lo
        try:
            imagem = await _baixar(emoji.url)
        except Exception as exc:  # rede ou HTTP: nunca gravar snapshot incompleto
            # Nunca grave um snapshot incompleto que depois possa apagar emojis válidos.
            raise RuntimeError(
                f"não foi possível incluir o emoji {emoji.name} no backup"
            ) from exc
        saida.append(
            {
                "id": str(emoji.id),
                "nome": emoji.name,
                "imagem": base64.b64encode(imagem).decode("ascii"),
                "animado": emoji.animated,
                "cargos": [str(role.id) for role in emoji.roles],
            }
        )
    return saida


def _canais_do_servidor(guild: discord.Guild) -> list:
    return [
        c
        for c in guild.channels
        if isinstance(
            c,
            (
                discord.TextChannel,
                discord.VoiceChannel,
                discord.StageChannel,
                discord.ForumChannel,
                discord.CategoryChannel,
            ),
        )
    ]


def _cargos_do_servidor(guild: discord.Guild) -> list[discord.Role]:
    return [r for r in guild.roles if not r.managed and r != guild.default_role]


async def _icone(guild: discord.Guild) -> tuple[str | None, str | None]:
    if guild.icon is None:
        return None, None
    try:
        dados = await guild.icon.read()
    except discord.HTTPException:
        return None, None
    return base64.b64encode(dados).decode(), guild.icon.key


def _thread_dados(thread: discord.Thread, arquivada: bool, membros: list[str]) -> dict:
    return {
        "id": str(thread.id),
        "nome": thread.name,
        "pai": str(thread.parent_id),
        "privado": thread.type == discord.ChannelType.private_thread,
        "arquivada": arquivada,
        "duracao": thread.auto_archive_duration,
        "membros": membros,
        "travada": thread.locked,
        "slowmode": thread.slowmode_delay,
        "convidavel": thread.invitable,
        "tags": [str(tag.id) for tag in getattr(thread, "applied_tags", [])],
    }


async def _membros_da_thread(thread: discord.Thread) -> list[str]:
    try:
        return [str(m.id) for m in await thread.fetch_members()]
    except discord.HTTPException as exc:
        raise RuntimeError(
            f"membros da thread {thread.id} não puderam ser listados"
        ) from exc


async def _threads_dados(guild: discord.Guild) -> list[dict]:
    """Threads ativas e arquivadas (públicas e privadas que o bot consegue ver), com quem está em cada uma.
    As mensagens dentro das threads não são guardadas."""
    saida, vistos = [], set()
    for thread in guild.threads:
        vistos.add(thread.id)
        saida.append(_thread_dados(thread, False, await _membros_da_thread(thread)))
    for canal in guild.channels:
        if not hasattr(canal, "archived_threads"):
            continue
        tipos = (False,) if isinstance(canal, discord.ForumChannel) else (False, True)
        for privado in tipos:
            try:
                async for thread in canal.archived_threads(
                    limit=None,
                    **(
                        {}
                        if isinstance(canal, discord.ForumChannel)
                        else {"private": privado}
                    ),
                ):
                    if thread.id in vistos:
                        continue
                    vistos.add(thread.id)
                    saida.append(
                        _thread_dados(thread, True, await _membros_da_thread(thread))
                    )
            except discord.HTTPException as exc:
                raise RuntimeError(
                    f"não foi possível listar threads arquivadas de {canal.name}; backup cancelado"
                ) from exc
    return saida


async def _membros_cargos_dados(guild: discord.Guild) -> dict:
    """Cargos de membros (menos @everyone e cargos gerenciados por integração)."""
    try:
        if not guild.chunked:
            await guild.chunk()
    except (discord.HTTPException, discord.ClientException) as exc:
        raise RuntimeError(
            "não foi possível listar todos os membros; backup cancelado"
        ) from exc
    return {
        str(m.id): [
            str(r.id) for r in m.roles if r != guild.default_role and not r.managed
        ]
        for m in guild.members
    }


async def _membros_perfil_dados(guild: discord.Guild) -> dict:
    """Metadados de perfil do membro que são do servidor; avatares globais não são copiados."""
    saida = {}
    for member in guild.members:
        saida[str(member.id)] = {
            "apelido": member.nick,
            "avatar_servidor": await _asset_dados(member.guild_avatar),
            "timeout_ate": member.timed_out_until.isoformat()
            if member.timed_out_until
            else None,
        }
    return saida


def listar() -> list[tuple[Path, dict]]:
    """Todos os backups, do mais novo para o mais antigo, já lidos."""
    saida = []
    for arq in reversed(arquivos()):
        try:
            saida.append((arq, _ler_backup(arq)))
        except (OSError, ValueError):
            log.warning("backup %s ilegível: ignorado", arq.name)
    return saida


def caminho_do(nome_arquivo: str) -> Path | None:
    """Só aceita um arquivo que esteja de fato na pasta de backups (evita caminhos inventados)."""
    for arq in arquivos():
        if arq.name == nome_arquivo:
            return arq
    return None


_IGNORADOS = {"criado_em", "motivo", "nome"}


def _sem_mudanca(antigo: dict, novo: dict) -> bool:
    """Compara o conteúdo do backup, sem data, motivo e nome."""

    def limpo(d):
        return {k: v for k, v in d.items() if k not in _IGNORADOS}

    return json.dumps(limpo(antigo), sort_keys=True) == json.dumps(
        limpo(novo), sort_keys=True
    )


def apagar(nome_arquivo: str) -> bool:
    """Apaga um backup da pasta. Só apaga arquivo que de fato existe na pasta de backups."""
    arq = caminho_do(nome_arquivo)
    from . import incidente

    if arq is None or arq.name == NOME_BACKUP_DIARIO:
        return False
    st = estado_restauracao()
    if st.get("arquivo") == nome_arquivo and st.get("status") in {"rodando", "aguardando", "erro", "parado"}:
        return False
    if incidente.ativo() and incidente.estado(settings.GUILD_ID).get("backup") == nome_arquivo:
        return False
    try:
        arq.unlink()
    except OSError:
        log.exception("backup %s não apagado", nome_arquivo)
        return False
    return True


async def _criar(
    guild: discord.Guild, motivo: str = "manual", nome: str | None = None
) -> Path | None:
    """Grava um backup agora. Não grava durante raid: o estado daquela hora não serve.
    Quem chama decide se registra no log (o diário não registra; o comando /backup registra)."""
    if em_raid() or _ocupado or estado_restauracao().get("status") in {
        "rodando", "aguardando", "erro", "parado"
    }:
        log.info("backup adiado: raid ou restauração em andamento")
        return None
    log.info("backup iniciado: motivo=%s guild=%s", motivo, guild.id)
    icone, icone_chave = await _icone(guild)
    dados = {
        "schema_version": 4,
        "guild_id": str(guild.id),
        "criado_em": datetime.now().isoformat(timespec="seconds"),
        "motivo": motivo,
        "nome": (nome or "").strip()[:60],
        "servidor": {
            "nome": guild.name,
            "descricao": guild.description,
            "icone": icone,
            "icone_chave": icone_chave,
            "everyone": guild.default_role.permissions.value,
            "banner": await _asset_dados(guild.banner),
            "banner_chave": guild.banner.key if guild.banner else None,
            "splash": await _asset_dados(guild.splash),
            "splash_chave": guild.splash.key if guild.splash else None,
            "discovery_splash": await _asset_dados(guild.discovery_splash),
            "discovery_splash_chave": guild.discovery_splash.key
            if guild.discovery_splash
            else None,
            "widget_enabled": guild.widget_enabled,
            "widget_channel": str(guild.widget_channel.id)
            if guild.widget_channel
            else None,
            "discoverable": "DISCOVERABLE" in guild.features,
            "premium_progress": guild.premium_progress_bar_enabled,
            "safety_alerts_channel": str(guild.safety_alerts_channel.id)
            if guild.safety_alerts_channel
            else None,
            "raid_alerts_disabled": getattr(guild, "raid_alerts_disabled", None),
            "invites_disabled": guild.invites_paused(),
            "mfa_level": guild.mfa_level.value,
            "afk_timeout": guild.afk_timeout,
            "afk_channel": str(guild.afk_channel.id) if guild.afk_channel else None,
            "default_notifications": guild.default_notifications.value,
            "verification_level": guild.verification_level.value,
            "explicit_content_filter": guild.explicit_content_filter.value,
            "system_channel": str(guild.system_channel.id)
            if guild.system_channel
            else None,
            "system_channel_flags": guild.system_channel_flags.value,
            "preferred_locale": str(guild.preferred_locale),
            "rules_channel": str(guild.rules_channel.id)
            if guild.rules_channel
            else None,
            "public_updates_channel": str(guild.public_updates_channel.id)
            if guild.public_updates_channel
            else None,
        },
        "cargos": [await _cargo_dados(r) for r in _cargos_do_servidor(guild)],
        "canais": [_canal_dados(c) for c in _canais_do_servidor(guild)],
        "emojis": await _emojis_dados(guild),
        "threads": await _threads_dados(guild),
        "membros_cargos": await _membros_cargos_dados(guild),
        "webhooks": await _webhooks_dados(guild),
        "convites": await _convites_dados(guild),
        "banimentos": await _banimentos_dados(guild),
        "figurinhas": await _figurinhas_dados(guild),
        "automod": await _automod_dados(guild),
        "eventos_programados": await _eventos_programados_dados(guild),
        "membros": await _membros_perfil_dados(guild),
    }
    # Só pede a tela se o servidor tem a feature: sem ela a API responde "não existe" (NotFound).
    if "COMMUNITY" in guild.features and "WELCOME_SCREEN_ENABLED" in guild.features:
        try:
            welcome = await guild.welcome_screen()
            dados["servidor"]["welcome_screen"] = {
                "enabled": welcome.enabled,
                "description": welcome.description,
                "channels": [
                    {
                        "canal": str(x.channel.id),
                        "descricao": x.description,
                        "emoji_id": str(x.emoji.id) if x.emoji and x.emoji.id else None,
                        "emoji_nome": x.emoji.name if x.emoji else None,
                        "emoji_unicode": str(x.emoji)
                        if x.emoji and not x.emoji.id
                        else None,
                    }
                    for x in welcome.welcome_channels
                ],
            }
        except discord.HTTPException as exc:
            # Opcional e sem risco no restore: sem tela de boas-vindas no backup, o restore só não mexe nela.
            log.warning("tela de boas-vindas não salva no backup (%s): %s", exc.status, exc.text)
            dados["servidor"].pop("welcome_screen", None)
    from . import mensagens

    dados["mensagens"] = await mensagens.snapshot(guild)
    if not dados["mensagens"]["completo"]:
        canais_com_falha = ", ".join(dados["mensagens"]["falhas"][:10])
        raise RuntimeError(
            f"histórico incompleto em {canais_com_falha}; backup cancelado"
        )
    if em_raid() or _ocupado or estado_restauracao().get("status") in {
        "rodando", "aguardando", "erro", "parado"
    }:
        raise RuntimeError(
            "Raid ou restauração detectada durante a coleta; backup descartado para preservar o estado confiável"
        )
    PASTA.mkdir(parents=True, exist_ok=True)
    if motivo == "diário":
        caminho = PASTA / NOME_BACKUP_DIARIO
        try:
            anterior = _ler_backup(caminho) if caminho.exists() else None
        except (OSError, ValueError, KeyError) as exc:
            log.warning("manifesto diário anterior ilegível; será substituído atomicamente: %s", exc)
            anterior = None
        if anterior is not None and _sem_mudanca(anterior, dados):
            log.info("backup diário: estado igual; atualizando o mesmo manifesto sem criar objetos novos")
    else:
        caminho = PASTA / f"backup-{datetime.now():%Y-%m-%d-%H%M%S-%f}.json"
    _gravar_incremental(caminho, dados)
    _limpar()
    if settings.GOOGLE_DRIVE_ENABLED:
        from . import google_drive

        google_drive.agendar_sincronizacao()
    log.info(
        "backup criado: %s (%d canais, %d cargos, %d threads, %d membros com cargos)",
        caminho.name,
        len(dados["canais"]),
        len(dados["cargos"]),
        len(dados["threads"]),
        len(dados["membros_cargos"]),
    )
    return caminho


async def criar(
    guild: discord.Guild, motivo: str = "manual", nome: str | None = None
) -> Path | None:
    """Serializa snapshots para que backup manual e diário não sobrescrevam estados concorrentes."""
    async with _trava_backup:
        return await _criar(guild, motivo=motivo, nome=nome)


# ── Restauração ───────────────────────────────────────────────────────────────


def _permissoes(guild: discord.Guild, mapa: dict, lista: list[dict], roles: dict | None = None) -> dict:
    saida = {}
    for ow in lista:
        if not isinstance(ow, dict):
            log.warning("overwrite ignorado: registro de permissão inválido no backup")
            continue
        tipo = ow.get("tipo")
        ident = ow.get("id")
        try:
            ident = int(ident)
        except (TypeError, ValueError):
            log.warning("overwrite ignorado: ID de alvo inválido no backup")
            continue
        if tipo == "cargo":
            # @everyone não faz parte da lista de cargos restauráveis. Ele
            # continua existindo com o ID do servidor, então resolvemos pelo
            # cargo padrão antes de consultar o mapa dos cargos comuns.
            if ident == guild.default_role.id:
                alvo = guild.default_role
            else:
                chave = str(ow.get("id"))
                alvo = (roles or {}).get(chave)
                novo = mapa.get(chave)
                if alvo is None and novo:
                    alvo = guild.get_role(int(novo))
                # Referência de um cargo ainda existente no servidor, mas que
                # não precisou de recriação nesta restauração.
                if alvo is None:
                    alvo = guild.get_role(ident)
        elif tipo == "membro":
            alvo = guild.get_member(ident)
        else:
            log.warning("overwrite ignorado: tipo de alvo desconhecido (%s)", tipo)
            continue
        if alvo is None:
            # Um cargo apagado pode não ser recriável por hierarquia/limite da
            # API; isso não deve impedir a criação do canal inteiro. Mantém-se
            # o máximo de overwrites válidos e registra-se só a referência perdida.
            log.warning(
                "overwrite ignorado: alvo %s %s não existe após reconciliar cargos",
                ow.get("tipo", "desconhecido"), ow.get("nome") or ow.get("id", "?"),
            )
            continue
        try:
            saida[alvo] = discord.PermissionOverwrite.from_pair(
                discord.Permissions(int(ow["allow"])), discord.Permissions(int(ow["deny"]))
            )
        except (KeyError, TypeError, ValueError):
            log.warning("overwrite ignorado: bits de permissão inválidos para %s", ow.get("nome") or ident)
    return saida


async def _servidor(guild: discord.Guild, dados: dict) -> list[str]:
    s = dados["servidor"]
    payload: dict = {}
    if guild.name != s["nome"]:
        payload["name"] = s["nome"]
    if (guild.description or "") != (s["descricao"] or ""):
        payload["description"] = s["descricao"] or ""
    for nome, campo, chave in (
        ("icon", "icone", "icone_chave"),
        ("banner", "banner", "banner_chave"),
        ("splash", "splash", "splash_chave"),
        ("discovery_splash", "discovery_splash", "discovery_splash_chave"),
    ):
        atual = getattr(guild, nome, None)
        if (atual.key if atual else None) != s.get(chave):
            bruto = s.get(campo)
            payload[nome] = base64.b64decode(bruto) if bruto else None
    feitos = []
    if payload:
        try:
            await guild.edit(reason="Restaurar backup", **payload)
            feitos.append("nome/descrição/ícone/banner")
        except (discord.HTTPException, TypeError, ValueError) as exc:
            log.warning("não consegui ajustar o servidor: %s", exc)
            feitos.append("falha ao restaurar nome/descrição/ícone/banner")
    return feitos


async def _config_servidor(guild: discord.Guild, s: dict, mapa: dict,
                           canais: dict | None = None) -> list[str]:
    """Restaura configurações de servidor depois que canais e IDs já foram reconciliados."""
    if "afk_timeout" not in s:  # backup legado: não inventa valores ausentes
        return []

    referencias_faltantes = set()

    def canal(chave):
        valor = s.get(chave)
        if not valor:
            return None
        destino = (canais or {}).get(str(valor))
        novo = mapa.get(str(valor))
        if destino is None and novo:
            destino = guild.get_channel(int(novo))
        if destino is None:
            referencias_faltantes.add(chave)
        return destino

    kwargs = {
        "afk_timeout": s["afk_timeout"],
        "afk_channel": canal("afk_channel"),
        "default_notifications": discord.NotificationLevel(s["default_notifications"]),
        "verification_level": discord.VerificationLevel(s["verification_level"]),
        "explicit_content_filter": discord.ContentFilter(s["explicit_content_filter"]),
        "system_channel": canal("system_channel"),
        "system_channel_flags": discord.SystemChannelFlags._from_value(
            s["system_channel_flags"]
        ),
        "preferred_locale": discord.Locale(s["preferred_locale"]),
        "rules_channel": canal("rules_channel"),
        "public_updates_channel": canal("public_updates_channel"),
    }
    for chave in referencias_faltantes:
        kwargs.pop(chave, None)
    for key, param in (
        ("widget_enabled", "widget_enabled"),
        ("premium_progress", "premium_progress_bar_enabled"),
        ("discoverable", "discoverable"),
    ):
        if key in s:
            kwargs[param] = s[key]
    if s.get("raid_alerts_disabled") is not None:
        kwargs["raid_alerts_disabled"] = s["raid_alerts_disabled"]
    if "mfa_level" in s:
        kwargs["mfa_level"] = discord.MFALevel(s["mfa_level"])
    atual = {
        "afk_timeout": guild.afk_timeout,
        "afk_channel": guild.afk_channel,
        "default_notifications": guild.default_notifications,
        "verification_level": guild.verification_level,
        "explicit_content_filter": guild.explicit_content_filter,
        "system_channel": guild.system_channel,
        "system_channel_flags": guild.system_channel_flags,
        "preferred_locale": guild.preferred_locale,
        "rules_channel": guild.rules_channel,
        "public_updates_channel": guild.public_updates_channel,
        "premium_progress_bar_enabled": guild.premium_progress_bar_enabled,
        "widget_enabled": guild.widget_enabled,
        "discoverable": "DISCOVERABLE" in guild.features,
        "mfa_level": guild.mfa_level,
    }
    if getattr(guild, "raid_alerts_disabled", None) is not None:
        atual["raid_alerts_disabled"] = guild.raid_alerts_disabled
    from . import incidente

    if "invites_disabled" in s and not incidente.ativo(guild.id):
        kwargs["invites_disabled"] = bool(s["invites_disabled"])
        atual["invites_disabled"] = guild.invites_paused()
    kwargs = {
        key: value for key, value in kwargs.items()
        if (getattr(value, "id", None) != getattr(atual.get(key), "id", None)
            if key.endswith("channel") or key in ("afk_channel", "system_channel", "rules_channel", "public_updates_channel")
            else value != atual.get(key))
    }
    feitos = []
    for chave in sorted(referencias_faltantes):
        log.warning("canal %s salvo no backup não foi remapeado", chave)
        feitos.append(f"falha ao remapear o canal {chave}")
    if kwargs:
        try:
            await guild.edit(**kwargs, reason="Restauração do backup")
            feitos.append("configurações gerais do servidor")
        except (discord.HTTPException, TypeError, ValueError, AttributeError) as exc:
            log.warning("não consegui restaurar todas as configurações gerais: %s", exc)
            feitos.append("algumas configurações gerais falharam")
    # O endpoint aceita null para limpar estas referências. Reconciliar também
    # o caso nulo evita conservar uma configuração que não existia no snapshot.
    for chave in ("widget_channel", "safety_alerts_channel"):
        valor = s.get(chave)
        alvo = canal(chave) if valor else None
        if valor and alvo is None:
            log.warning("canal %s salvo no backup não foi remapeado", chave)
            feitos.append(f"falha ao remapear o canal {chave}")
            continue
        atual_id = getattr(getattr(guild, chave, None), "id", None)
        alvo_id = getattr(alvo, "id", None) if alvo is not None else None
        if atual_id == alvo_id:
            continue
        try:
            await guild.edit(**{chave: alvo}, reason="Restauração do backup")
        except (discord.HTTPException, TypeError, ValueError, AttributeError) as exc:
            log.warning("%s não restaurado: %s", chave, exc)
            feitos.append(f"falha ao restaurar {chave}")
    # Restore the community welcome screen after mapping its channel and emoji references.
    if "welcome_screen" in s and "WELCOME_SCREEN_ENABLED" in guild.features:
        try:
            current = await guild.welcome_screen()
            channels = []
            for item in s["welcome_screen"].get("channels", []):
                cid = mapa.get(item["canal"])
                if not cid:
                    raise ValueError(f"canal do backup {item['canal']} não foi remapeado")
                channel_obj = guild.get_channel(int(cid))
                if channel_obj is None:
                    raise ValueError(f"canal {cid} da tela de boas-vindas não existe")
                emoji = item.get("emoji_unicode")
                if item.get("emoji_id"):
                    old = item["emoji_id"]
                    novo = mapa.get(f"emoji:{old}")
                    if not novo:
                        raise ValueError(f"emoji {old} não foi remapeado")
                    emoji = discord.PartialEmoji(
                        name=item.get("emoji_nome") or "emoji",
                        id=int(novo),
                    )
                channels.append(
                    discord.WelcomeChannel(
                        channel=channel_obj, description=item["descricao"], emoji=emoji
                    )
                )
            def emoji_key(value):
                if value is None:
                    return None
                emoji_id = getattr(value, "id", None)
                return ("custom", int(emoji_id)) if emoji_id else ("unicode", str(value))

            wanted_channels = [
                (item.channel.id, item.description, emoji_key(item.emoji))
                for item in channels
            ]
            current_channels = [
                (getattr(getattr(item, "channel", None), "id", None), item.description, emoji_key(item.emoji))
                for item in current.welcome_channels
            ]
            wanted_description = s["welcome_screen"].get("description") or ""
            wanted_enabled = s["welcome_screen"].get("enabled", False)
            if (current.description or "") != wanted_description or current_channels != wanted_channels or current.enabled != wanted_enabled:
                await current.edit(
                    description=wanted_description,
                    welcome_channels=channels,
                    enabled=wanted_enabled,
                    reason="Restaurar backup",
                )
        except (discord.HTTPException, ValueError, TypeError) as exc:
            log.warning("tela de boas-vindas não restaurada: %s", exc)
            feitos.append("falha ao restaurar tela de boas-vindas")
    if guild.default_role.permissions.value != s.get("everyone"):
        try:
            await guild.default_role.edit(
                permissions=discord.Permissions(s["everyone"]), reason="Backup"
            )
            feitos.append("permissões do @everyone")
        except discord.HTTPException as exc:
            log.warning("não consegui ajustar @everyone: %s", exc)
            feitos.append("permissões do @everyone falharam")
    return feitos


def _parear_cargos(dados, guild):
    atuais = [r for r in guild.roles if r != guild.default_role and not r.managed]

    def nome(valor):
        return (valor["nome"] if isinstance(valor, dict) else valor.name).casefold()

    def campo(valor, salvo, atributo):
        return valor[salvo] if isinstance(valor, dict) else getattr(valor, atributo)

    def assinatura(valor):
        return (
            int(campo(valor, "permissoes", "permissions").value)
            if not isinstance(valor, dict)
            else int(valor["permissoes"]),
            int(campo(valor, "cor", "colour").value)
            if not isinstance(valor, dict)
            else int(valor["cor"]),
            bool(campo(valor, "destaque", "hoist")),
            bool(campo(valor, "mencionavel", "mentionable")),
            campo(valor, "cor_secundaria", "secondary_colour").value
            if not isinstance(valor, dict) and getattr(valor, "secondary_colour", None)
            else valor.get("cor_secundaria") if isinstance(valor, dict) else None,
            campo(valor, "cor_terciaria", "tertiary_colour").value
            if not isinstance(valor, dict) and getattr(valor, "tertiary_colour", None)
            else valor.get("cor_terciaria") if isinstance(valor, dict) else None,
        )

    def posicao(valor):
        return int(valor.get("posicao", 0)) if isinstance(valor, dict) else int(valor.position)

    desejados_por_nome = defaultdict(list)
    atuais_por_nome = defaultdict(list)
    for i, role in enumerate(dados):
        desejados_por_nome[nome(role)].append(i)
    for j, role in enumerate(atuais):
        atuais_por_nome[nome(role)].append(j)

    pares = {}
    usados_desejados = set()
    usados_atuais = set()

    def parear(indices_d, indices_a):
        # A multiplicidade e a assinatura das permissões/cor/posição decidem
        # os pares. IDs de cargos antigos nunca entram na comparação.
        ds = sorted(indices_d, key=lambda i: posicao(dados[i]))
        ars = sorted(indices_a, key=lambda j: posicao(atuais[j]))
        while ds and ars:
            i = ds.pop(0)
            j = min(
                ars,
                key=lambda x: (
                    sum(a != b for a, b in zip(assinatura(dados[i]), assinatura(atuais[x]))) * 10_000
                    + abs(posicao(dados[i]) - posicao(atuais[x]))
                ),
            )
            ars.remove(j)
            pares[i] = j
            usados_desejados.add(i)
            usados_atuais.add(j)

    for key, indices_d in desejados_por_nome.items():
        parear(indices_d, atuais_por_nome.get(key, []))

    # Se um cargo foi renomeado, só o remapeamos quando a assinatura restante
    # é suficientemente específica e única; caso contrário, recriamos o salvo.
    sobras_d = [i for i in range(len(dados)) if i not in usados_desejados]
    sobras_a = [j for j in range(len(atuais)) if j not in usados_atuais]
    grupos_d = defaultdict(list)
    grupos_a = defaultdict(list)
    for i in sobras_d:
        grupos_d[assinatura(dados[i])].append(i)
    for j in sobras_a:
        grupos_a[assinatura(atuais[j])].append(j)
    for key, indices_d in grupos_d.items():
        indices_a = grupos_a.get(key, [])
        if indices_a:
            parear(indices_d, indices_a)

    return (
        {dados[i]["id"]: atuais[j] for i, j in pares.items()},
        False,
        [atuais[j] for j in range(len(atuais)) if j not in usados_atuais],
    )


def _parear_canais(dados, guild):
    atuais = _canais_do_servidor(guild)
    categorias = {c["id"]: c["nome"] for c in dados if c["tipo"] == "categoria"}
    def parent(c):
        if isinstance(c, dict):
            return categorias.get(c.get("pai"))
        return c.category.name if c.category else None
    def props(c):
        if isinstance(c, dict):
            return (c["tipo"], c["nome"], parent(c), c.get("topico"), c.get("posicao"))
        return (next((t for t in ("categoria", "palco", "forum", "voz", "texto") if _tipo_bate(c, t)), None), c.name, parent(c), getattr(c, "topic", None), c.position)
    def score(c, ch):
        return (9 if c["nome"] == ch.name else 0) + (6 if parent(c) == parent(ch) else 0) + (3 if c.get("topico") == getattr(ch, "topic", None) else 0) + (2 if c.get("posicao") == ch.position else 0)

    # Canais repetidos são válidos no Discord. O reconciliador antigo deixava
    # grupos indistinguíveis como ambíguos; por segurança, isso bloqueava a
    # remoção de TODOS os extras. Em um nuke com centenas de canais iguais,
    # a restauração então recriava os ausentes e mantinha os canais do ataque.
    # Para itens com a mesma identidade semântica, a quantidade do snapshot é
    # a fonte da verdade. Fazemos o casamento um-a-um pela posição mais próxima
    # (sem usar IDs) e mantemos como extras os excedentes do grupo.
    def normalizar(valor):
        return " ".join(str(valor or "").casefold().split())

    def chave(canal, salvo):
        tipo, nome, _, _, _ = props(canal)
        return tipo, normalizar(nome)

    desejados_por_chave = defaultdict(list)
    atuais_por_chave = defaultdict(list)
    for i, item in enumerate(dados):
        desejados_por_chave[chave(item, True)].append(i)
    for j, item in enumerate(atuais):
        atuais_por_chave[chave(item, False)].append(j)

    pares = {}
    usados_desejados = set()
    usados_atuais = set()

    def parear_ordenado(indices_d, indices_a):
        """Casa o menor grupo por custo de posição, preservando excedentes."""
        ds = sorted(indices_d, key=lambda i: (int(dados[i].get("posicao", 0)), i))
        ars = sorted(indices_a, key=lambda j: (int(atuais[j].position), j))
        # Programação dinâmica de sequência: casa todos do grupo menor e
        # escolhe o subconjunto mais próximo quando há canais excedentes.
        transposto = len(ds) > len(ars)
        esquerda, direita = (ars, ds) if transposto else (ds, ars)

        def custo(a, b):
            di, aj = (b, a) if transposto else (a, b)
            desejado, atual = dados[di], atuais[aj]
            diferenca = abs(
                int(desejado.get("posicao", 0)) - int(atual.position)
            )
            custo = min(diferenca, 500)
            if normalizar(parent(desejado)) != normalizar(parent(atual)):
                custo += 10_000
            if normalizar(desejado.get("topico")) != normalizar(
                getattr(atual, "topic", None)
            ):
                custo += 2_000
            return custo

        n, m = len(esquerda), len(direita)
        inf = 10**12
        dp = [[inf] * (m + 1) for _ in range(n + 1)]
        escolha = [[None] * (m + 1) for _ in range(n + 1)]
        for j in range(m + 1):
            dp[0][j] = 0
        for i in range(1, n + 1):
            for j in range(i, m + 1):
                pular = dp[i][j - 1]
                casar = dp[i - 1][j - 1] + custo(esquerda[i - 1], direita[j - 1])
                if casar <= pular:
                    dp[i][j], escolha[i][j] = casar, "casar"
                else:
                    dp[i][j], escolha[i][j] = pular, "pular"
        selecionados = []
        i, j = n, m
        while i:
            if j <= 0:
                break
            if escolha[i][j] == "casar":
                selecionados.append((esquerda[i - 1], direita[j - 1]))
                i -= 1
                j -= 1
            else:
                j -= 1
        for a, b in reversed(selecionados):
            di, aj = (b, a) if transposto else (a, b)
            pares[di] = aj
            usados_desejados.add(di)
            usados_atuais.add(aj)

    for identidade, indices_d in desejados_por_chave.items():
        indices_a = atuais_por_chave.get(identidade, [])
        if indices_a:
            parear_ordenado(indices_d, indices_a)

    restantes_d = [i for i in range(len(dados)) if i not in usados_desejados]
    restantes_a = [j for j in range(len(atuais)) if j not in usados_atuais]

    def compatibilidade(c, ch):
        return (
            _tipo_bate(ch, c["tipo"])
            and (
                normalizar(c.get("nome")) == normalizar(ch.name)
                or (
                    bool(normalizar(c.get("topico")))
                    and normalizar(c.get("topico")) == normalizar(getattr(ch, "topic", None))
                    and normalizar(parent(c)) == normalizar(parent(ch))
                )
            )
        )

    pares_restantes, ambiguos, extras = reconciliar.parear(
        [dados[i] for i in restantes_d],
        [atuais[j] for j in restantes_a],
        props,
        compatibilidade,
        score,
        minimo=9,
    )
    for i, j in pares_restantes.items():
        di, aj = restantes_d[i], restantes_a[j]
        pares[di] = aj
        usados_desejados.add(di)
        usados_atuais.add(aj)
    extras_indices = [restantes_a[j] for j in extras]
    # O reconciliador devolve todo item sem par nesse conjunto, inclusive os
    # realmente ausentes. Só é ambíguo quando ainda existe candidato compatível.
    ambiguos_reais = any(
        any(
            compatibilidade(dados[restantes_d[i]], atuais[restantes_a[j]])
            for j in extras
        )
        for i in ambiguos
    )
    return (
        {dados[i]["id"]: atuais[j] for i, j in pares.items()},
        ambiguos_reais,
        [atuais[j] for j in extras_indices],
    )


async def _cargos(guild: discord.Guild, dados: dict, mapa: dict, roles: dict | None = None) -> list[str]:
    feitos = []
    pares, ambiguos, extras = _parear_cargos(dados["cargos"], guild)
    resolvidos = set()
    for c in dados["cargos"]:
        alvo = pares.get(c["id"])
        try:
            if alvo is None:
                alvo = await guild.create_role(
                    name=c["nome"],
                    permissions=discord.Permissions(c["permissoes"]),
                    colour=discord.Colour(c["cor"]),
                    hoist=c["destaque"],
                    display_icon=base64.b64decode(c["icone"])
                    if c.get("icone")
                    else c.get("emoji"),
                    secondary_colour=c.get("cor_secundaria"),
                    tertiary_colour=c.get("cor_terciaria"),
                    mentionable=c["mencionavel"],
                    reason="Backup",
                )
                feitos.append(f"cargo {c['nome']} recriado")
            elif (
                alvo.permissions.value,
                alvo.name,
                alvo.colour.value,
                alvo.hoist,
                alvo.mentionable,
                alvo.secondary_colour.value if alvo.secondary_colour else None,
                alvo.tertiary_colour.value if alvo.tertiary_colour else None,
                await _asset_dados(alvo.display_icon)
                if isinstance(alvo.display_icon, discord.Asset)
                else alvo.display_icon,
            ) != (
                c["permissoes"],
                c["nome"],
                c["cor"],
                c["destaque"],
                c["mencionavel"],
                c.get("cor_secundaria"),
                c.get("cor_terciaria"),
                c.get("icone") or c.get("emoji"),
            ):
                await alvo.edit(
                    name=c["nome"],
                    permissions=discord.Permissions(c["permissoes"]),
                    colour=discord.Colour(c["cor"]),
                    hoist=c["destaque"],
                    mentionable=c["mencionavel"],
                    display_icon=base64.b64decode(c["icone"])
                    if c.get("icone")
                    else c.get("emoji"),
                    secondary_colour=c.get("cor_secundaria"),
                    tertiary_colour=c.get("cor_terciaria"),
                    reason="Backup",
                )
                feitos.append(f"cargo {c['nome']} ajustado")
            resolvidos.add(c["id"])
            mapa[c["id"]] = str(alvo.id)
            if roles is not None:
                roles[c["id"]] = alvo
        except discord.HTTPException as exc:
            log.warning("cargo %s não restaurado: %s", c["nome"], exc)
    if dados.get("schema_version", 1) >= 2 and not ambiguos and all(
        c["id"] in resolvidos for c in dados["cargos"]
    ):
        for cargo in extras:
            try:
                await cargo.delete(reason="Não existia no backup restaurado")
                feitos.append(f"cargo extra {cargo.name} removido")
            except discord.HTTPException as exc:
                log.warning("cargo extra %s não removido: %s", cargo.name, exc)
                feitos.append(f"falha ao remover cargo extra {cargo.name}")
        posicoes = {
            (roles or {})[c["id"]]: int(c.get("posicao", i + 1))
            for i, c in enumerate(dados["cargos"])
            if c["id"] in resolvidos and c["id"] in (roles or {})
        }
        if posicoes and any(role.position != pos for role, pos in posicoes.items()):
            try:
                await guild.edit_role_positions(
                    positions=posicoes, reason="Ordem de cargos do backup"
                )
                feitos.append("ordem dos cargos restaurada")
            except discord.HTTPException as exc:
                log.warning("ordem dos cargos não restaurada: %s", exc)
                feitos.append("falha ao restaurar ordem dos cargos")
    return feitos


def _difere(canal, chave: str, novo) -> bool:
    """Compara o valor atual do canal com o do backup. Sem diferença, não há PATCH."""
    if chave == "overwrites":
        return True  # a comparação já foi feita antes de chegar aqui
    if chave == "position":
        return canal.position != novo
    if chave == "available_tags":
        return [t.name for t in canal.available_tags] != [t.name for t in novo]
    atual = getattr(canal, chave, _SEM_VALOR)
    if atual is _SEM_VALOR:
        return True
    if chave == "default_reaction_emoji":
        return str(atual) != str(novo)
    return atual != novo


async def _limpar_canais_extras(guild: discord.Guild, dados: dict, mapa: dict) -> tuple[list[str], list[str]]:
    """Remove primeiro canais que excedem o snapshot, antes de recriar estrutura.

    Em grupos com nomes repetidos, o matcher escolhe correspondências um-a-um
    pelo contexto e posição. Todo item atual que sobra sem par é removido para
    que a ausência real possa ser recriada na fase estrutural.
    """
    pares, _ambiguos, extras = _parear_canais(dados.get("canais", []), guild)
    feitos: list[str] = []
    falhas: list[str] = []
    for antigo, canal in pares.items():
        mapa[antigo] = str(canal.id)

    # `_parear_canais` já faz casamento um-a-um: canais indistinguíveis são
    # limitados pela multiplicidade do snapshot, e candidatos não pareados são
    # excedentes. Uma ambiguidade de atributos não deve deixar recursos de raid
    # no servidor nem impedir a recriação do item salvo.
    candidatos = list(extras)

    # Canais-filhos primeiro; categorias por último.
    candidatos.sort(key=lambda c: isinstance(c, discord.CategoryChannel))
    async def apagar(canal):
        try:
            await _excluir_canal_limitado(canal, "Restore: canal ausente do snapshot")
            return f"canal extra #{canal.name} removido"
        except discord.NotFound:
            return None
        except discord.HTTPException as exc:
            return f"falha ao remover #{canal.name}: {type(exc).__name__}"

    for inicio in range(0, len(candidatos), 25):
        resultados = await asyncio.gather(
            *(apagar(canal) for canal in candidatos[inicio:inicio + 25])
        )
        for resultado in resultados:
            if not resultado:
                continue
            if resultado.startswith("falha"):
                falhas.append(resultado)
                log.warning("%s", resultado)
            else:
                feitos.append(resultado)
        log.info(
            "pré-limpeza de canais: %d/%d candidatos processados; removidos=%d falhas=%d",
            min(inicio + 25, len(candidatos)), len(candidatos), len(feitos), len(falhas),
        )
    if candidatos:
        log.info(
            "pré-limpeza de canais: removidos=%d falhas=%d",
            len(feitos), len(falhas),
        )
    return feitos, falhas


async def _limpar_threads_extras(guild: discord.Guild, dados: dict, mapa: dict) -> tuple[list[str], list[str]]:
    """Reconcilia tópicos por canal-pai, nome, tipo e propriedades, sem IDs antigos."""
    global _cache_threads_restore
    atuais = {str(t.id): t for t in guild.threads}
    completo = True
    for canal in guild.channels:
        if not hasattr(canal, "archived_threads"):
            continue
        privados = (False,) if isinstance(canal, discord.ForumChannel) else (False, True)
        for privado in privados:
            try:
                async for thread in canal.archived_threads(
                    limit=None,
                    **({} if isinstance(canal, discord.ForumChannel) else {"private": privado}),
                ):
                    atuais[str(thread.id)] = thread
            except discord.HTTPException as exc:
                completo = False
                log.warning("tópicos arquivados de %s indisponíveis na pré-limpeza: %s", canal.id, exc)

    def chave(pai, nome, privado):
        return str(pai), " ".join(str(nome or "").casefold().split()), bool(privado)

    desejados_por_chave = defaultdict(list)
    atuais_por_chave = defaultdict(list)
    for t in dados.get("threads", []):
        pai_atual = mapa.get(t["pai"], f"ausente:{t['pai']}")
        desejados_por_chave[chave(pai_atual, t["nome"], t["privado"])].append(t)
    for t in atuais.values():
        atuais_por_chave[chave(t.parent_id, t.name, t.type == discord.ChannelType.private_thread)].append(t)

    usados: set[str] = set()
    mapa_threads: dict[str, str] = {}
    for identidade, esperados in desejados_por_chave.items():
        candidatos = atuais_por_chave.get(identidade, [])
        restantes = list(candidatos)
        for salvo in esperados:
            if not restantes:
                break

            def custo(thread):
                return (
                    int(bool(thread.archived) != bool(salvo.get("arquivada"))) * 100
                    + int(bool(thread.locked) != bool(salvo.get("travada"))) * 50
                    + abs(int(getattr(thread, "slowmode_delay", 0)) - int(salvo.get("slowmode", 0)))
                    + int(getattr(thread, "auto_archive_duration", 0) != salvo.get("duracao")) * 10
                )

            atual = min(
                restantes,
                key=lambda t: (custo(t), getattr(t, "created_at", None) or discord.utils.utcnow()),
            )
            restantes.remove(atual)
            usados.add(str(atual.id))
            mapa_threads[salvo["id"]] = str(atual.id)
            mapa[salvo["id"]] = str(atual.id)

    extras = [t for tid, t in atuais.items() if tid not in usados]
    contagem_desejada = Counter({k: len(v) for k, v in desejados_por_chave.items()})
    contagem_atual = Counter({k: len(v) for k, v in atuais_por_chave.items()})
    candidatos_remover = [
        t for t in extras
        if completo or contagem_atual[chave(t.parent_id, t.name, t.type == discord.ChannelType.private_thread)]
        > contagem_desejada[chave(t.parent_id, t.name, t.type == discord.ChannelType.private_thread)]
    ]
    sem = asyncio.Semaphore(settings.INCIDENTE_CONCORRENCIA_EXCLUSOES)

    async def apagar(thread):
        async with sem:
            try:
                await thread.delete(reason="Restore: tópico ausente do snapshot")
                atuais.pop(str(thread.id), None)
                return f"tópico extra {thread.name} removido"
            except discord.NotFound:
                atuais.pop(str(thread.id), None)
                return None
            except discord.HTTPException as exc:
                return f"falha ao remover tópico {thread.name}: {type(exc).__name__}"

    feitos, falhas = [], []
    for inicio in range(0, len(candidatos_remover), 25):
        resultados = await asyncio.gather(*(apagar(t) for t in candidatos_remover[inicio:inicio + 25]))
        for resultado in resultados:
            if not resultado:
                continue
            if resultado.startswith("falha"):
                falhas.append(resultado)
                log.warning("%s", resultado)
            else:
                feitos.append(resultado)
    if not completo:
        falhas.append(
            "listagem de tópicos arquivados incompleta; restore não pode confirmar a ausência de extras"
        )
        log.warning("restore de tópicos incompleto: listagem de arquivados não terminou")
    _cache_threads_restore = (guild.id, time.monotonic(), atuais, completo)
    return feitos, falhas


async def _canais(guild: discord.Guild, dados: dict, mapa: dict,
                  roles: dict | None = None, canais: dict | None = None) -> list[str]:
    """Serializa a etapa estrutural para evitar mudanças de canal conflitantes."""
    async with _trava_canais:
        return await _canais_em_fila(guild, dados, mapa, roles, canais)


async def _canais_em_fila(guild: discord.Guild, dados: dict, mapa: dict,
                          roles: dict | None = None, canais: dict | None = None) -> list[str]:
    feitos = []
    objetos_resolvidos: dict[str, discord.abc.GuildChannel] = {}
    reordenar = False
    pares, _ambiguos, extras = _parear_canais(dados["canais"], guild)
    resolvidos_canais = set()
    # Ordem original (posição salva). Sem posição no backup, usa a posição atual do servidor.
    ordenados = regras.ordem_canais(dados["canais"], _posicoes_atuais(guild, dados, mapa))
    for c in ordenados:
        atual = pares.get(c["id"])
        pai_id = mapa.get(c["pai"]) if c["pai"] else None
        pai = ((canais or {}).get(c["pai"]) or guild.get_channel(int(pai_id))) if pai_id else None
        try:
            if c["pai"] and pai is None:
                raise ValueError(f"categoria do backup {c['pai']} não foi remapeada")
            overwrites = _permissoes(guild, mapa, c["permissoes"], roles)
            if atual is not None:
                mapa[c["id"]] = str(atual.id)
                objetos_resolvidos[c["id"]] = atual
                if atual.position != c["posicao"] or (
                    c["tipo"] != "categoria"
                    and getattr(atual, "category_id", None) != getattr(pai, "id", None)
                ):
                    reordenar = True
                if canais is not None:
                    canais[c["id"]] = atual
                if c["tipo"] == "forum":
                    atuais_tags = {tag.name: tag for tag in atual.available_tags}
                    for tag in c.get("tags", []):
                        if tag["nome"] in atuais_tags:
                            mapa[f"forum_tag:{tag['id']}"] = str(
                                atuais_tags[tag["nome"]].id
                            )
                if atual.name != c["nome"] or (
                    c["tipo"] != "categoria"
                    and getattr(atual, "category_id", None) != getattr(pai, "id", None)
                ):
                    await atual.edit(
                        name=c["nome"],
                        **({} if c["tipo"] == "categoria" else {"category": pai}),
                        reason="Backup",
                    )
                    feitos.append(f"canal {c['nome']} ajustado")
                    await asyncio.sleep(PAUSA_ENTRE_CANAIS_S)
                if c["tipo"] in ("texto", "forum") and getattr(
                    atual, "topic", None
                ) != c.get("topico"):
                    await atual.edit(topic=c["topico"], reason="Backup")
                    feitos.append(f"tópico de {c['nome']} ajustado")
                    await asyncio.sleep(PAUSA_ENTRE_CANAIS_S)
                # Posições são enviadas juntas no endpoint bulk do Discord
                # após reconciliar canais; PATCH individual por canal gerava
                # rajadas na rota /guilds/{id}/channels.
                attrs = {}
                if c["tipo"] == "texto":
                    attrs.update(
                        nsfw=c.get("nsfw", False),
                        slowmode_delay=c.get("lento", 0),
                        default_auto_archive_duration=c.get("arquivo_padrao", 1440),
                    )
                elif c["tipo"] in ("voz", "palco"):
                    attrs.update(
                        bitrate=min(c.get("bitrate", 64000), guild.bitrate_limit),
                        user_limit=c.get("limite", 0),
                        rtc_region=c.get("regiao"),
                        video_quality_mode=discord.VideoQualityMode(
                            c.get("qualidade", 1)
                        ),
                    )
                elif c["tipo"] == "forum":
                    tags = [_tag_de_backup(x, mapa) for x in c.get("tags", [])]
                    attrs.update(
                        nsfw=c.get("nsfw", False),
                        slowmode_delay=c.get("lento", 0),
                        default_auto_archive_duration=c.get("arquivo_padrao", 1440),
                        default_thread_slowmode_delay=c.get("lento_threads", 0),
                        default_layout=discord.ForumLayoutType(c.get("layout", 0)),
                        default_sort_order=discord.ForumOrderType(
                            c.get("ordenacao", 0)
                        ),
                        default_reaction_emoji=_emoji_de_backup(
                            c["reacao_padrao"], mapa
                        )
                        if c.get("reacao_padrao")
                        else None,
                        available_tags=tags,
                    )
                if atual.overwrites != overwrites:
                    attrs["overwrites"] = overwrites
                # Só manda o que mudou; sem diferença nenhuma, nem chama a API.
                attrs = {k: v for k, v in attrs.items() if _difere(atual, k, v)}
                if attrs:
                    await atual.edit(**attrs, reason="Backup")
                    await asyncio.sleep(PAUSA_ENTRE_CANAIS_S)
                resolvidos_canais.add(c["id"])
                continue
            if c["tipo"] == "categoria":
                novo = await guild.create_category(
                    c["nome"], overwrites=overwrites, reason="Backup"
                )
            elif c["tipo"] == "voz":
                novo = await guild.create_voice_channel(
                    c["nome"],
                    overwrites=overwrites,
                    category=pai,
                    bitrate=min(c.get("bitrate") or 64000, guild.bitrate_limit),
                    user_limit=c.get("limite") or 0,
                    rtc_region=c.get("regiao"),
                    video_quality_mode=discord.VideoQualityMode(c.get("qualidade", 1)),
                    reason="Backup",
                )
            elif c["tipo"] == "palco":
                novo = await guild.create_stage_channel(
                    c["nome"],
                    overwrites=overwrites,
                    category=pai,
                    bitrate=min(c.get("bitrate") or 64000, guild.bitrate_limit),
                    user_limit=c.get("limite") or 0,
                    rtc_region=c.get("regiao"),
                    video_quality_mode=discord.VideoQualityMode(c.get("qualidade", 1)),
                    reason="Backup",
                )
            elif c["tipo"] == "forum":
                novo = await guild.create_forum(
                    c["nome"],
                    overwrites=overwrites,
                    category=pai,
                    topic=c.get("topico"),
                    nsfw=c.get("nsfw", False),
                    slowmode_delay=c.get("lento") or 0,
                    default_auto_archive_duration=c.get("arquivo_padrao", 1440),
                    default_thread_slowmode_delay=c.get("lento_threads", 0),
                    default_layout=discord.ForumLayoutType(c.get("layout", 0)),
                    default_sort_order=discord.ForumOrderType(c.get("ordenacao", 0)),
                    available_tags=[
                        discord.ForumTag(
                            name=x["nome"],
                            emoji=x.get("emoji"),
                            moderated=x.get("moderada", False),
                        )
                        for x in c.get("tags", [])
                    ],
                    reason="Backup",
                )
            else:
                novo = await guild.create_text_channel(
                    c["nome"],
                    overwrites=overwrites,
                    category=pai,
                    topic=c.get("topico"),
                    nsfw=c.get("nsfw", False),
                    slowmode_delay=c.get("lento") or 0,
                    default_auto_archive_duration=c.get("arquivo_padrao", 1440),
                    reason="Backup",
                )
            mapa[c["id"]] = str(novo.id)
            objetos_resolvidos[c["id"]] = novo
            reordenar = True
            if canais is not None:
                canais[c["id"]] = novo
            resolvidos_canais.add(c["id"])
            await asyncio.sleep(PAUSA_ENTRE_CANAIS_S)
            feitos.append(f"canal {c['nome']} recriado")
            if c["tipo"] == "forum":
                atuais_tags = {tag.name: tag for tag in novo.available_tags}
                for tag in c.get("tags", []):
                    if tag["nome"] in atuais_tags:
                        mapa[f"forum_tag:{tag['id']}"] = str(
                            atuais_tags[tag["nome"]].id
                        )
        except (discord.HTTPException, ValueError) as exc:
            log.warning("canal %s não restaurado: %s", c["nome"], exc)
            feitos.append(f"falha em {c['nome']}")
    if reordenar:
        posicoes_alteradas = [
            {"id": objetos_resolvidos[c["id"]].id, "position": int(c["posicao"])}
            for c in dados["canais"]
            if c["id"] in objetos_resolvidos
            and getattr(objetos_resolvidos[c["id"]], "position", None) != int(c["posicao"])
        ]
        try:
            if posicoes_alteradas:
                await rest.atualizar_posicoes_canais(guild.id, posicoes_alteradas)
                feitos.append(f"posições de {len(posicoes_alteradas)} canais ajustadas")
        except (rest.DiscordHTTPError, rest.DiscordTransportError) as exc:
            log.warning("posições dos canais não restauradas em lote: %s", exc)
            feitos.append("falha ao restaurar posições dos canais")
    if dados.get("schema_version", 1) >= 2 and all(
        c["id"] in resolvidos_canais for c in dados["canais"]
    ):
        # Remove todo recurso sem par no snapshot; filhos primeiro.
        extras.sort(key=lambda ch: isinstance(ch, discord.CategoryChannel))
        for canal_extra in extras:
            try:
                await _excluir_canal_limitado(canal_extra, "Não existia no backup restaurado")
                feitos.append(f"canal extra #{canal_extra.name} removido")
                await asyncio.sleep(PAUSA_ENTRE_CANAIS_S)
            except discord.HTTPException as exc:
                log.warning("canal extra %s não removido: %s", canal_extra.name, exc)
                feitos.append(f"falha ao remover canal extra #{canal_extra.name}")
    return feitos


async def _adicionar_membro_thread(thread: discord.Thread, membro: discord.Member) -> None:
    """Serializa o endpoint de associação a threads e evita re-adicionar membros já presentes."""
    global _ultima_adicao_membro_thread
    if thread.get_member(membro.id) is not None:
        return
    async with _trava_membros_threads:
        if thread.get_member(membro.id) is not None:
            return
        espera = INTERVALO_ADICAO_MEMBRO_THREAD_S - (
            time.monotonic() - _ultima_adicao_membro_thread
        )
        if espera > 0:
            await asyncio.sleep(espera)
        try:
            await thread.add_user(membro)
        finally:
            _ultima_adicao_membro_thread = time.monotonic()


async def _threads(guild: discord.Guild, dados: dict, mapa: dict,
                   canais: dict | None = None) -> list[str]:
    """Recria as threads que sumiram, põe de volta os membros e arquiva as que estavam arquivadas.
    Só recria threads dentro de canais de texto que existam; forum e stage não entram no backup."""
    feitos = []
    cache = _cache_threads_restore
    usar_cache = bool(cache and cache[0] == guild.id and time.monotonic() - cache[1] < 120)
    ativas = dict(cache[2]) if usar_cache else {str(t.id): t for t in guild.threads}
    if not usar_cache:
        ativas.update({str(t.id): t for t in guild.threads})
    # A pré-limpeza já paginou as arquivadas; reutiliza o resultado e evita
    # repetir dezenas de consultas à API no mesmo restore.
    pesquisa_completa = cache[3] if usar_cache else True
    if not usar_cache:
        for canal in guild.channels:
            if not hasattr(canal, "archived_threads"):
                continue
            for privado in ((False,) if isinstance(canal, discord.ForumChannel) else (False, True)):
                try:
                    async for thread in canal.archived_threads(limit=None, **({} if isinstance(canal, discord.ForumChannel) else {"private": privado})):
                        ativas[str(thread.id)] = thread
                except discord.HTTPException as exc:
                    pesquisa_completa = False
                    log.warning("threads arquivadas de %s indisponíveis: %s", canal.id, exc)
    usadas_threads = set()
    threads_resolvidas = set()
    for t in dados["threads"]:
        pai_id = mapa.get(t["pai"])
        pai = ((canais or {}).get(t["pai"]) or guild.get_channel(int(pai_id))) if pai_id else None
        if not isinstance(pai, (discord.TextChannel, discord.ForumChannel)):
            log.warning("pai do tópico %s indisponível", t["nome"])
            continue
        try:
            candidates = [x for x in ativas.values()
                          if x.id not in usadas_threads and x.parent_id == pai.id
                          and x.name == t["nome"]
                          and (x.type == discord.ChannelType.private_thread) == t["privado"]]
            atual = None
            mapeado = mapa.get(t["id"])
            if mapeado:
                candidato = ativas.get(str(mapeado))
                if candidato in candidates:
                    atual = candidato
            if atual is None and candidates:
                # A repetição legítima de nomes não suspende a restauração. O
                # snapshot define a multiplicidade e as propriedades escolhem
                # o par mais semelhante sem depender do ID da thread.
                atual = min(candidates, key=lambda x: (
                    int(bool(x.archived) != bool(t["arquivada"])) * 100
                    + int(bool(x.locked) != bool(t.get("travada"))) * 50
                    + abs(int(getattr(x, "slowmode_delay", 0)) - int(t.get("slowmode", 0)))
                    + int(getattr(x, "auto_archive_duration", 0) != t["duracao"]) * 10,
                    getattr(x, "created_at", None) or discord.utils.utcnow(),
                ))
            if atual is None:
                tipo = (
                    discord.ChannelType.private_thread
                    if t["privado"]
                    else discord.ChannelType.public_thread
                )
                if isinstance(pai, discord.ForumChannel):
                    resultado = await pai.create_thread(
                        name=t["nome"],
                        content="Restaurando histórico…",
                        auto_archive_duration=t["duracao"],
                        slowmode_delay=t.get("slowmode", 0),
                        reason="Backup",
                    )
                    atual = resultado.thread
                else:
                    atual = await pai.create_thread(
                        name=t["nome"],
                        type=tipo,
                        auto_archive_duration=t["duracao"],
                        slowmode_delay=t.get("slowmode", 0),
                        invitable=t.get("convidavel", True),
                        reason="Backup",
                    )
                feitos.append(f"tópico {t['nome']} recriado")
            ativas[str(atual.id)] = atual
            usadas_threads.add(atual.id)
            mapa[t["id"]] = str(atual.id)
            if atual.name != t["nome"]:
                await atual.edit(name=t["nome"], reason="Backup")
            for uid in t["membros"]:
                membro = guild.get_member(int(uid))
                if membro is None or membro.bot:
                    continue
                try:
                    await _adicionar_membro_thread(atual, membro)
                except discord.HTTPException as exc:
                    log.warning("membro %s não adicionado ao tópico: %s", uid, exc)
            parent_data = next(
                (c for c in dados.get("canais", []) if c["id"] == t["pai"]), None
            )
            if parent_data and parent_data["tipo"] == "forum":
                tagmap = {tag["id"]: tag for tag in parent_data.get("tags", [])}
                tags = [
                    pai.get_tag(int(mapa[f"forum_tag:{tid}"]))
                    for tid in t.get("tags", [])
                    if tid in tagmap and f"forum_tag:{tid}" in mapa
                ]
                if len(tags) != len(t.get("tags", [])):
                    raise ValueError(f"tags do tópico {t['nome']} não foram remapeadas")
                tags = [tag for tag in tags if tag is not None]
                if tags and list(atual.applied_tags) != tags:
                    await atual.edit(applied_tags=tags, reason="Backup")
            if getattr(atual, "slowmode_delay", 0) != t.get("slowmode", 0):
                await atual.edit(slowmode_delay=t.get("slowmode", 0), reason="Backup")
            if t["arquivada"] and (
                not getattr(atual, "archived", False)
                or ("travada" in t and getattr(atual, "locked", False) != t["travada"])
            ):
                await atual.edit(
                    archived=True, locked=t.get("travada", False), reason="Backup"
                )
            threads_resolvidas.add(t["id"])
        except discord.HTTPException as exc:
            log.warning("tópico %s não restaurado: %s", t["nome"], exc)
            feitos.append(f"falha no tópico {t['nome']}")
    if dados.get("schema_version", 1) >= 3 and pesquisa_completa and all(
        t["id"] in threads_resolvidas for t in dados.get("threads", [])
    ):
        desejados = {mapa[t["id"]] for t in dados.get("threads", [])}
        for tid, thread in ativas.items():
            if tid not in desejados:
                try:
                    await thread.delete(reason="Não existia no backup")
                except discord.HTTPException as exc:
                    log.warning("tópico extra %s não removido: %s", thread.name, exc)
    return feitos


async def _membros_cargos(guild: discord.Guild, dados: dict, mapa: dict,
                           roles: dict | None = None) -> list[str]:
    """Sincroniza cargos humanos ao snapshot; em schema 2 também remove cargos extras."""
    snapshot = dados.get("membros_cargos", {})
    if dados.get("schema_version", 1) >= 3:
        try:
            if not guild.chunked:
                await guild.chunk()
        except (discord.HTTPException, discord.ClientException):
            log.warning(
                "lista incompleta de membros; restauração de cargos não será destrutiva"
            )
            return ["cargos de membros não sincronizados: lista incompleta"]
        membros = list(guild.members)
    else:
        membros = [
            m
            for uid in snapshot
            if (m := guild.get_member(int(uid))) is not None and not m.bot
        ]
    alterados = 0
    from . import incidente
    contidos = set(incidente.estado(guild.id).get("adicionadores_contidos", [])) if incidente.ativo(guild.id) else set()
    cargo_membro_id_salvo = store.get_config("cargo_membro_id")
    cargo_membro_backup = next(
        (c for c in dados.get("cargos", [])
         if c.get("id") == cargo_membro_id_salvo or c.get("nome") == settings.CARGO_MEMBRO),
        None,
    )
    cargo_membro = None
    if cargo_membro_backup:
        rid = cargo_membro_backup["id"]
        cargo_membro = (roles or {}).get(rid)
        if cargo_membro is None:
            log.warning("cargo-base de membro %s não foi remapeado", rid)

    for membro in membros:
        if incidente.alvo(guild.id, membro.id) or str(membro.id) in contidos:
            # Alvos contidos não recebem os cargos do snapshot, mas conservam
            # o cargo-base de membro depois que ele próprio foi recriado.
            if cargo_membro and cargo_membro not in membro.roles and cargo_membro.is_assignable():
                try:
                    await membro.add_roles(cargo_membro, reason="Anti-raid: manter cargo-base de membro")
                except discord.HTTPException as exc:
                    log.warning("cargo-base não reatribuído ao alvo contido %s: %s", membro.id, exc)
            continue
        ids = snapshot.get(str(membro.id), [])
        desejados = []
        for rid in ids:
            cargo = (roles or {}).get(rid)
            if cargo is None:
                log.warning("cargo %s do membro %s não foi remapeado", rid, membro.id)
                continue
            if not cargo.managed and cargo < guild.me.top_role:
                desejados.append(cargo)
        desejados_set = set(desejados)
        adicionar = [r for r in desejados if r not in membro.roles]
        remover = (
            [
                r
                for r in membro.roles
                if r != guild.default_role
                and not r.managed
                and r not in desejados_set
                and r < guild.me.top_role
            ]
            if dados.get("schema_version", 1) >= 3
            else []
        )
        try:
            if adicionar:
                await membro.add_roles(*adicionar, reason="Restauração do backup")
            if remover:
                await membro.remove_roles(
                    *remover, reason="Não existia no backup restaurado"
                )
            if adicionar or remover:
                alterados += 1
        except discord.HTTPException as exc:
            log.warning("cargos de %s não sincronizados: %s", membro, exc)
    perfis = dados.get("membros", {})
    for membro in membros:
        if incidente.alvo(guild.id, membro.id) or str(membro.id) in contidos:
            continue
        perfil = perfis.get(str(membro.id))
        if not perfil:
            continue
        try:
            timeout = (
                datetime.fromisoformat(perfil["timeout_ate"])
                if perfil.get("timeout_ate")
                else None
            )
            if timeout and timeout <= discord.utils.utcnow():
                timeout = None
            avatar = (
                base64.b64decode(perfil["avatar_servidor"])
                if perfil.get("avatar_servidor")
                else None
            )
            avatar_atual = await _asset_dados(membro.guild_avatar)
            needs_edit = (
                membro.nick != perfil.get("apelido")
                or avatar_atual != perfil.get("avatar_servidor")
                or (membro.timed_out_until is not None and timeout is None)
                or (timeout is not None and membro.timed_out_until != timeout)
            )
            if needs_edit:
                await membro.edit(
                    nick=perfil.get("apelido"),
                    timed_out_until=timeout,
                    avatar=avatar,
                    reason="Restaurar perfil do membro",
                )
        except discord.HTTPException as exc:
            log.warning("perfil de %s não restaurado: %s", membro.id, exc)
    return [f"cargos sincronizados de {alterados} membros"] if alterados else []


async def _emojis(guild: discord.Guild, dados: dict, mapa: dict,
                  roles: dict | None = None) -> list[str]:
    """Repõe emojis do backup e remove emojis criados depois do snapshot."""
    if dados.get("schema_version", 1) < 2 or "emojis" not in dados:
        return []
    feitos = []
    usados = set()
    falha = False
    imagens_atuais: dict[int, bytes] = {}
    for item in dados["emojis"]:
        try:
            imagem = base64.b64decode(item["imagem"], validate=True)
            candidatos = [e for e in guild.emojis if not e.managed and e.id not in usados]
            iguais = []
            for e in candidatos:
                if e.id not in imagens_atuais:
                    imagens_atuais[e.id] = await e.read()
                if imagens_atuais[e.id] == imagem:
                    iguais.append(e)
            if len(iguais) > 1:
                mesmo_nome = [e for e in iguais if e.name == item["nome"]]
                if len(mesmo_nome) == 1:
                    iguais = mesmo_nome
            if len(iguais) > 1:
                falha = True
                log.warning("emoji %s ambíguo; preservando candidatos", item["nome"])
                continue
            emoji = iguais[0] if iguais else None
            roles_emoji = [(roles or {}).get(rid) for rid in item.get("cargos", [])]
            if any(r is None for r in roles_emoji):
                raise ValueError(f"cargos de acesso do emoji {item['nome']} não foram remapeados")
            if emoji is None:
                emoji = await guild.create_custom_emoji(name=item["nome"], image=imagem,
                                                        roles=roles_emoji, reason="Restauração do backup")
                feitos.append(f"emoji :{item['nome']}: recriado")
            elif emoji.name != item["nome"] or {r.id for r in emoji.roles} != {r.id for r in roles_emoji}:
                await emoji.edit(name=item["nome"], roles=roles_emoji, reason="Restauração do backup")
                feitos.append(f"emoji :{item['nome']}: ajustado")
            usados.add(emoji.id)
            mapa[f"emoji:{item['id']}"] = str(emoji.id)
        except (discord.HTTPException, ValueError, KeyError) as exc:
            falha = True
            log.warning("emoji %s não restaurado: %s", item.get("nome"), exc)
            feitos.append(f"falha no emoji :{item.get('nome', '?')}:")
    # Só remove extras se todas as entradas do backup foram recuperadas.
    if not falha and len(usados) == len(dados["emojis"]):
        for emoji in list(guild.emojis):
            if emoji.id in usados or emoji.managed:
                continue
            try:
                await emoji.delete(reason="Não existia no backup restaurado")
                feitos.append(f"emoji extra :{emoji.name}: removido")
            except discord.HTTPException as exc:
                log.warning("emoji extra %s não removido: %s", emoji.name, exc)
                feitos.append(f"falha ao remover emoji :{emoji.name}:")
    return feitos


async def _banimentos(guild: discord.Guild, dados: dict) -> list[str]:
    if "banimentos" not in dados:
        return []
    feitos = []
    from . import incidente

    protected = (
        {int(uid) for uid in incidente.estado(guild.id).get("alvos", {})}
        if incidente.ativo(guild.id)
        else set()
    )
    atual = {entry.user.id: entry async for entry in guild.bans(limit=None)}
    desejados = {int(x["user_id"]): x for x in dados["banimentos"]}
    for uid, entry in atual.items():
        if uid in desejados or uid in protected:
            continue
        try:
            await guild.unban(entry.user, reason="Não estava banido no backup")
            feitos.append(f"ban removido de {uid}")
        except discord.HTTPException as exc:
            log.warning("ban extra de %s não removido: %s", uid, exc)
            feitos.append(f"falha ao remover ban de {uid}")
    for uid, item in desejados.items():
        if uid in atual or uid in protected:
            continue
        try:
            await guild.ban(
                discord.Object(id=uid),
                reason=item.get("motivo") or "Ban restaurado do backup",
                delete_message_seconds=0,
            )
            feitos.append(f"ban restaurado para {uid}")
        except discord.HTTPException as exc:
            log.warning("ban de %s não restaurado: %s", uid, exc)
            feitos.append(f"falha ao restaurar ban de {uid}")
    return feitos


async def _convites(guild: discord.Guild, dados: dict, mapa: dict,
                    canais: dict | None = None) -> list[str]:
    if "convites" not in dados:
        return []
    feitos = []
    from . import incidente

    s = incidente.estado(guild.id)
    revogar = {int(x) for x in s.get("revogar_convites", [])}
    atuais = await guild.invites()
    preservados = set()
    from . import raid

    validadores: dict[int, bool] = {}

    async def criador_presente(uid: int | None) -> bool:
        if uid is None:
            return True
        uid = int(uid)
        if uid not in validadores:
            validadores[uid] = await raid._criador_presente(guild, uid)
        return validadores[uid]

    # Pareia pelo canal atual e propriedades; o código antigo pode ter mudado
    # em uma restauração anterior e não define a identidade do convite.
    for item in dados["convites"]:
        code = item.get("codigo", "(sem código)")
        creator = int(item["criador"]) if item.get("criador") else None
        if not await criador_presente(creator):
            feitos.append(f"convite {code} não restaurado: criador ausente do servidor")
            continue
        if creator in revogar:
            feitos.append(f"convite {code} revogado pela política anti-raid")
            continue
        channel_id = mapa.get(str(item.get("canal"))) if item.get("canal") else None
        channel = ((canais or {}).get(str(item.get("canal")))
                   or (guild.get_channel(int(channel_id)) if channel_id else None))
        if channel is None:
            feitos.append(f"falha ao restaurar convite {code}: canal ausente")
            continue
        max_age = int(item.get("max_age") or 0)
        if max_age and item.get("expira_em"):
            expires = datetime.fromisoformat(item["expira_em"])
            max_age = min(
                max_age, max(0, int((expires - discord.utils.utcnow()).total_seconds()))
            )
            if max_age == 0:
                continue
        max_uses = int(item.get("max_uses") or 0)
        target_type_value = item.get("target_type")
        target_user_id = str(item.get("target_user") or "")
        target_application = str(item.get("target_application") or "")
        candidates = [
            invite for invite in atuais
            if invite.code not in preservados
            and getattr(getattr(invite, "channel", None), "id", None) == channel.id
            and str(getattr(getattr(invite, "inviter", None), "id", "") or "") == str(creator or "")
            and int(getattr(invite, "max_age", 0) or 0) == int(item.get("max_age") or 0)
            and int(getattr(invite, "max_uses", 0) or 0) == max_uses
            and bool(getattr(invite, "temporary", False)) == bool(item.get("temporario"))
            and (invite.target_type.value if invite.target_type else None) == target_type_value
            and str(getattr(getattr(invite, "target_user", None), "id", "") or "") == target_user_id
            and str(getattr(getattr(invite, "target_application", None), "id", "") or "") == target_application
        ]
        if candidates:
            candidates.sort(key=lambda invite: invite.created_at or discord.utils.utcnow())
            preservados.add(candidates[0].code)
            continue
        try:
            target_type = (
                discord.InviteTarget(item["target_type"])
                if item.get("target_type")
                else None
            )
            target_user = (
                await context.bot().fetch_user(int(item["target_user"]))
                if item.get("target_user")
                else None
            )
            new = await channel.create_invite(
                max_age=max_age,
                max_uses=max_uses,
                temporary=bool(item.get("temporario")),
                unique=True,
                target_type=target_type,
                target_user=target_user,
                target_application_id=item.get("target_application"),
                reason="Restaurar configurações de convite do backup",
            )
            feitos.append(f"convite {code} recriado como {new.code} (uso reiniciado)")
        except discord.HTTPException as exc:
            log.warning("convite %s não restaurado: %s", code, exc)
            feitos.append(f"falha ao recriar convite {code}")
    # Tudo que não foi pareado com o snapshot sai, inclusive convites que
    # conservaram o código mas mudaram de canal ou de configuração.
    for invite in atuais:
        if invite.code in preservados:
            continue
        try:
            await invite.delete(
                reason="Convite não existia no snapshot ou foi invalidado pela raid"
            )
        except discord.HTTPException as exc:
            feitos.append(f"falha ao apagar convite extra {invite.code}")
            log.warning("convite extra %s não removido: %s", invite.code, exc)
    return feitos


async def _webhooks(guild: discord.Guild, dados: dict, mapa: dict,
                    canais: dict | None = None) -> list[str]:
    if "webhooks" not in dados:
        return []
    feitos, usados = [], set()
    from ..data import store
    consultas = {}

    for item in dados["webhooks"]:
        channel_id = mapa.get(item["canal"])
        channel = ((canais or {}).get(item["canal"])
                   or (guild.get_channel(int(channel_id)) if channel_id else None))
        if channel is None or not hasattr(channel, "webhooks"):
            feitos.append(f"falha no webhook {item['nome']}: canal não existe")
            continue
        try:
            if channel.id not in consultas:
                consultas[channel.id] = await channel.webhooks()
            existentes = consultas[channel.id]
            candidates = [w for w in existentes if w.type == discord.WebhookType.incoming
                          and w.name == item["nome"] and w.id not in usados]
            if len(candidates) > 1:
                criador = str(item.get("criador") or "")
                mesmo_criador = [
                    w for w in candidates
                    if str(getattr(getattr(w, "user", None), "id", "")) == criador
                ]
                if mesmo_criador:
                    candidates = mesmo_criador
                # Webhooks podem compartilhar nome; a multiplicidade do backup
                # e a data de criação fazem o pareamento um-a-um sem IDs antigos.
                candidates.sort(key=lambda w: (
                    getattr(w, "created_at", None) or discord.utils.utcnow(),
                    str(getattr(getattr(w, "user", None), "id", "")),
                ))
            webhook = candidates[0] if candidates else None
            avatar = base64.b64decode(item["icone"]) if item.get("icone") else None
            if webhook is None:
                webhook = await channel.create_webhook(
                    name=item["nome"], avatar=avatar, reason="Restaurar backup"
                )
                existentes.append(webhook)
                feitos.append(f"webhook {item['nome']} recriado")
            elif webhook.name != item["nome"] or await _asset_dados(webhook.avatar) != item.get("icone"):
                await webhook.edit(
                    name=item["nome"], avatar=avatar, reason="Restaurar backup"
                )
            usados.add(webhook.id)
            if item.get("criador"):
                store.save_webhook_creator(str(webhook.id), int(item["criador"]))
        except (discord.HTTPException, ValueError) as exc:
            log.warning("webhook %s não restaurado: %s", item["nome"], exc)
            feitos.append(f"falha ao restaurar webhook {item['nome']}")
    if len(usados) != len(dados["webhooks"]):
        return feitos
    for channel in _canais_do_servidor(guild):
        if not hasattr(channel, "webhooks"):
            continue
        try:
            for webhook in await channel.webhooks():
                if (
                    webhook.type == discord.WebhookType.incoming
                    and webhook.id not in usados
                    and not (webhook.name == "Ghost backup" and webhook.user
                             and webhook.user.id == context.bot().user.id)
                ):
                    await webhook.delete(reason="Webhook extra ausente no backup")
                    feitos.append(f"webhook extra {webhook.name} removido")
        except discord.HTTPException as exc:
            log.warning("limpeza de webhooks em %s falhou: %s", channel.id, exc)
            feitos.append(f"falha ao limpar webhooks do canal {channel.id}")
    return feitos


async def _figurinhas(guild: discord.Guild, dados: dict) -> list[str]:
    if "figurinhas" not in dados:
        return []
    feitos, usados = [], set()
    atuais = {s.id: s for s in await guild.fetch_stickers() if s.guild_id == guild.id}
    for item in dados["figurinhas"]:
        candidates = [x for x in atuais.values()
                      if x.name == item["nome"] and x.id not in usados]
        data = base64.b64decode(item["imagem"], validate=True)
        if len(candidates) > 1:
            iguais = []
            for candidato in candidates:
                try:
                    if await _baixar(candidato.url) == data:
                        iguais.append(candidato)
                except (discord.HTTPException, RuntimeError) as exc:
                    log.warning("não consegui comparar a figurinha %s: %s", candidato.name, exc)
            if iguais:
                candidates = iguais
            else:
                metadata = [
                    x for x in candidates
                    if (x.description or "", x.emoji or "")
                    == (item.get("descricao") or "", item.get("emoji") or "")
                ]
                if metadata:
                    candidates = metadata
                if len(candidates) > 1:
                    feitos.append(f"falha na figurinha {item['nome']}: correspondência não identificável")
                    log.warning("figurinha %s tem candidatos indistinguíveis; extras preservados", item["nome"])
                    continue
        sticker = candidates[0] if candidates else None
        try:
            if sticker is not None and await _baixar(sticker.url) != data:
                await sticker.delete(reason="Arquivo da figurinha mudou desde o backup")
                sticker = None
            if sticker is None:
                ext = {1: "png", 2: "apng", 3: "json"}.get(item["formato"], "png")
                sticker = await guild.create_sticker(
                    name=item["nome"],
                    description=item.get("descricao") or "",
                    emoji=item.get("emoji") or "😀",
                    file=discord.File(io.BytesIO(data), filename=f"sticker.{ext}"),
                    reason="Restaurar backup",
                )
                feitos.append(f"figurinha {item['nome']} recriada")
            elif (sticker.name, sticker.description, sticker.emoji) != (
                item["nome"],
                item.get("descricao") or "",
                item.get("emoji") or "",
            ):
                await sticker.edit(
                    name=item["nome"],
                    description=item.get("descricao") or "",
                    emoji=item.get("emoji") or "😀",
                    reason="Restaurar backup",
                )
            usados.add(sticker.id)
        except (discord.HTTPException, ValueError, KeyError) as exc:
            log.warning("figurinha %s não restaurada: %s", item.get("nome"), exc)
            feitos.append(f"falha ao restaurar figurinha {item.get('nome', '?')}")
    if len(usados) == len(dados["figurinhas"]):
        for sticker in atuais.values():
            if sticker.id not in usados:
                try:
                    await sticker.delete(reason="Figurinha extra ausente no backup")
                    feitos.append(f"figurinha extra {sticker.name} removida")
                except discord.HTTPException as exc:
                    log.warning(
                        "figurinha extra %s não removida: %s", sticker.name, exc
                    )
                    feitos.append(f"falha ao remover figurinha extra {sticker.name}")
    return feitos


async def _automod(guild: discord.Guild, dados: dict, mapa: dict,
                   roles_resolvidos: dict | None = None,
                   canais_resolvidos: dict | None = None) -> list[str]:
    if "automod" not in dados:
        return []
    feitos, usados = [], set()
    existentes = await guild.fetch_automod_rules()

    def role_atual(old_id):
        alvo = (roles_resolvidos or {}).get(str(old_id))
        if alvo is None:
            raise ValueError(f"cargo {old_id} não foi remapeado")
        return alvo

    def canal_atual(old_id):
        if not old_id:
            return None
        old_id = str(old_id)
        novo_id = mapa.get(old_id)
        alvo = (canais_resolvidos or {}).get(old_id)
        if alvo is None and novo_id:
            alvo = guild.get_channel(int(novo_id))
        if alvo is None:
            raise ValueError(f"canal {old_id} não foi remapeado")
        return alvo

    for item in dados["automod"]:
        candidates = [r for r in existentes if r.name == item["nome"] and r.id not in usados]
        if len(candidates) > 1:
            feitos.append(f"falha na regra AutoMod {item['nome']}: nome ambíguo")
            continue
        regra = candidates[0] if candidates else None
        t = item["gatilho"]
        try:
            trigger = discord.AutoModTrigger(
                type=discord.AutoModRuleTriggerType(t["tipo"]),
                keyword_filter=t.get("keywords"),
                presets=discord.AutoModPresets._from_value(
                    [i + 1 for i in range(3) if t.get("presets", 0) & (1 << i)]
                ),
                allow_list=t.get("allow_list"),
                mention_limit=t.get("mention_limit"),
                regex_patterns=t.get("regex"),
                mention_raid_protection=t.get("mention_raid_protection"),
            )
            actions = [
                discord.AutoModRuleAction(
                    type=discord.AutoModRuleActionType(a["tipo"]),
                    channel_id=canal_atual(a["canal"]).id
                    if a.get("canal")
                    else None,
                    duration=timedelta(seconds=a["duracao"])
                    if a.get("duracao")
                    else None,
                    custom_message=a.get("mensagem"),
                )
                for a in item["acoes"]
            ]
            roles = [
                role_atual(x)
                for x in item["cargos_isentos"]
            ]
            channels = [
                canal_atual(x)
                for x in item["canais_isentos"]
            ]
            if regra is None:
                regra = await guild.create_automod_rule(
                    name=item["nome"],
                    event_type=discord.AutoModRuleEventType(item["evento"]),
                    trigger=trigger,
                    actions=actions,
                    enabled=item["habilitada"],
                    exempt_roles=roles,
                    exempt_channels=channels,
                    reason="Restaurar backup",
                )
                feitos.append(f"regra AutoMod {item['nome']} recriada")
            else:
                atual_trigger = regra.trigger
                atual_gatilho = {
                    "tipo": atual_trigger.type.value,
                    "keywords": atual_trigger.keyword_filter,
                    "presets": atual_trigger.presets.value if atual_trigger.presets else 0,
                    "allow_list": atual_trigger.allow_list,
                    "mention_limit": atual_trigger.mention_limit,
                    "regex": atual_trigger.regex_patterns,
                    "mention_raid_protection": atual_trigger.mention_raid_protection,
                }
                atual_acoes = [(a.type.value, a.channel_id,
                                a.duration.total_seconds() if a.duration else None,
                                a.custom_message) for a in regra.actions]
                desejadas_acoes = [(a["tipo"], canal_atual(a["canal"]).id
                                    if a.get("canal") else None, a.get("duracao"), a.get("mensagem"))
                                   for a in item["acoes"]]
                if (regra.event_type.value != item["evento"] or atual_gatilho != t
                    or atual_acoes != desejadas_acoes or regra.enabled != item["habilitada"]
                    or set(regra.exempt_role_ids) != {r.id for r in roles}
                    or set(regra.exempt_channel_ids) != {c.id for c in channels}):
                    await regra.edit(name=item["nome"],
                        event_type=discord.AutoModRuleEventType(item["evento"]),
                        trigger=trigger, actions=actions, enabled=item["habilitada"],
                        exempt_roles=roles, exempt_channels=channels,
                        reason="Restaurar backup")
            usados.add(regra.id)
        except (discord.HTTPException, TypeError, ValueError) as exc:
            log.warning("regra AutoMod %s não restaurada: %s", item["nome"], exc)
            feitos.append(f"falha na regra AutoMod {item['nome']}")
    if len(usados) == len(dados["automod"]):
        for rule in existentes:
            if rule.id not in usados:
                try:
                    await rule.delete(reason="Regra ausente no backup")
                except discord.HTTPException as exc:
                    log.warning("regra AutoMod extra não removida: %s", exc)
                    feitos.append(f"falha ao remover regra AutoMod {rule.name}")
    return feitos


async def _eventos_programados(
    guild: discord.Guild, dados: dict, mapa: dict,
    canais_resolvidos: dict | None = None,
) -> list[str]:
    if "eventos_programados" not in dados:
        return []
    feitos, usados = [], set()
    ativos_processados = 0
    atuais = await guild.fetch_scheduled_events(with_counts=False)
    for item in dados["eventos_programados"]:
        encerrado = int(item["estado"]) in (
            discord.EventStatus.completed.value,
            discord.EventStatus.canceled.value,
        )
        candidates = [e for e in atuais if e.name == item["nome"]
                      and e.id not in usados and e.start_time.isoformat() == item["inicio"]]
        if len(candidates) > 1:
            feitos.append(f"falha no evento {item['nome']}: ambíguo")
            log.warning("evento %s tem correspondências ambíguas", item["nome"])
            continue
        event = candidates[0] if candidates else None
        if encerrado:
            if event is not None:
                usados.add(event.id)
            else:
                feitos.append(f"falha no evento encerrado {item['nome']}: não pode ser recriado no passado")
                log.warning("evento encerrado %s ausente; a API não permite recriá-lo no passado", item["nome"])
            continue
        try:
            start = datetime.fromisoformat(item["inicio"])
            end = datetime.fromisoformat(item["fim"]) if item.get("fim") else None
            if end is not None and end <= discord.utils.utcnow():
                if event is not None:
                    usados.add(event.id)
                    ativos_processados += 1
                else:
                    feitos.append(f"falha no evento expirado {item['nome']}: não pode ser recriado no passado")
                    log.warning("evento expirado %s ausente; não pode ser recriado", item["nome"])
                continue
            channel_id = mapa.get(str(item["canal"])) if item.get("canal") else None
            channel = ((canais_resolvidos or {}).get(str(item.get("canal")))
                       or guild.get_channel(int(channel_id))) if channel_id else None
            if item.get("canal") and channel is None:
                raise ValueError(f"canal do evento {item['nome']} não foi remapeado")
            tipo = discord.EntityType(item["tipo"])
            kwargs = {
                "name": item["nome"],
                "description": item.get("descricao") or "",
                "start_time": start,
                "end_time": end,
                "privacy_level": discord.PrivacyLevel(item["privacidade"]),
                "entity_type": tipo,
                "image": base64.b64decode(item["imagem"])
                if item.get("imagem")
                else None,
                "reason": "Restaurar backup",
            }
            if tipo == discord.EntityType.external:
                kwargs["location"] = item.get("local")
            else:
                if channel is None:
                    raise ValueError("canal do evento ausente")
                kwargs["channel"] = channel
            if event is None:
                if start <= discord.utils.utcnow():
                    raise ValueError(
                        "evento iniciado não pode ser recriado com o mesmo estado"
                    )
                event = await guild.create_scheduled_event(**kwargs)
                feitos.append(f"evento {item['nome']} recriado")
            else:
                imagem_atual = await _asset_dados(event.cover_image)
                if (event.name != item["nome"] or (event.description or "") != (item.get("descricao") or "")
                    or event.start_time != start or event.end_time != end
                    or event.entity_type.value != item["tipo"] or event.privacy_level.value != item["privacidade"]
                    or (event.channel_id or None) != (channel.id if channel else None)
                    or (event.location or None) != (item.get("local") or None)
                    or imagem_atual != item.get("imagem")):
                    await event.edit(**kwargs)
            usados.add(event.id)
            ativos_processados += 1
        except (discord.HTTPException, TypeError, ValueError) as exc:
            log.warning("evento %s não restaurado: %s", item["nome"], exc)
            feitos.append(f"falha no evento {item['nome']}")
    ativos_desejados = sum(
        int(x["estado"]) not in (
            discord.EventStatus.completed.value,
            discord.EventStatus.canceled.value,
        )
        for x in dados["eventos_programados"]
    )
    if ativos_processados != ativos_desejados:
        return feitos
    for event in atuais:
        if event.id not in usados:
            try:
                await event.delete(reason="Evento extra ausente no backup")
            except discord.HTTPException as exc:
                feitos.append(f"falha ao remover evento {event.name}")
                log.warning("evento extra %s não removido: %s", event.name, exc)
    return feitos


def _tipo_bate(canal, tipo: str) -> bool:
    if tipo == "categoria":
        return isinstance(canal, discord.CategoryChannel)
    if tipo == "voz":
        return isinstance(canal, discord.VoiceChannel)
    if tipo == "palco":
        return isinstance(canal, discord.StageChannel)
    if tipo == "forum":
        return isinstance(canal, discord.ForumChannel)
    return isinstance(canal, discord.TextChannel)


def _remapear_config(mapa: dict) -> list[str]:
    """Atualiza configurações, dados persistidos e mensagens de setup para os canais recriados."""
    trocas = {antigo: novo for antigo, novo in mapa.items() if antigo != novo}
    if not trocas:
        return []
    avisos = []
    from ..database.connection import connection

    with connection() as conn:
        for old, new in trocas.items():
            for table in (
                "threads_ativas",
                "pedidos",
                "fechamentos_agendados",
                "restock_pedidos",
            ):
                conn.execute(
                    f"UPDATE {table} SET thread_id=? WHERE thread_id=?", (new, old)
                )
    for key, val in store.todas_configs().items():
        if key.startswith("cargo_") and key.endswith("_id") and val in trocas:
            store.set_config(key, trocas[val])
        if key in ("cargos_niveis", "cargos_niveis_compras", "cargos_niveis_mensagens"):
            levels = json.loads(val)
            for level in levels:
                if str(level.get("cargo_id")) in trocas:
                    level["cargo_id"] = trocas[str(level["cargo_id"])]
            store.set_config(key, json.dumps(levels, ensure_ascii=False))
    changed_catalogue = False
    for p in catalogo.produtos(so_ativos=False):
        doc = catalogo.doc_produto(p.id)
        if str(doc.get("canal")) in trocas:
            doc["canal"] = trocas[str(doc["canal"])]
            catalogo.gravar(doc)
            changed_catalogue = True
    if changed_catalogue:
        catalogo.recarregar()
    chaves = {k for k in settings.EDITAVEIS if k.endswith("_channel_id")}
    chaves |= {f"{v.id.lower()}_channel_id" for v in catalogo.vitrines()}
    for chave in sorted(chaves):
        valor, origem = store.config_atual(chave)
        if valor not in trocas:
            continue
        if origem == "banco":
            store.set_config(chave, trocas[valor])
            avisos.append(f"{chave} atualizado")
    # Atualiza somente variáveis de canal que já existem fisicamente no .env.
    # Não usa os.environ para decidir isso: ele também pode conter variáveis
    # exportadas pelo sistema que não devem ser materializadas no arquivo.
    for chave_env, atual in sorted(envs.valores_existentes("_CHANNEL_ID").items()):
        if not atual or atual not in trocas:
            continue
        novo = trocas[atual]
        if not envs.gravar_se_existente(chave_env, novo):
            continue
        atributo = "AGENT_CANAL_ID" if chave_env == "AGENT_CHANNEL_ID" else chave_env
        if hasattr(settings, atributo):
            setattr(settings, atributo, int(novo))  # vale já, sem reiniciar
        avisos.append(f"{chave_env} atualizado no .env")
    canal_agente = store.get_config("agente_canal") or ""
    if canal_agente in trocas:
        store.set_config("agente_canal", trocas[canal_agente])
        avisos.append("canal do Ghost Agent atualizado")
    for v in catalogo.vitrines():
        salva = store.setup_message(v.id)
        if salva and str(salva["channel_id"]) in trocas:
            store.save_setup_message(v.id, int(trocas[str(salva["channel_id"])]), int(salva["message_id"]))
            avisos.append(f"vitrine {v.id}: canal remapeado; mensagem será conferida")
    return avisos


# ── Restauração em fases ─────────────────────────────────────────────────────
#
# Ordem: estrutura (servidor, cargos, canais na posição salva, threads com membros, cargos, convites)
# → vitrines (conferidas/reparadas pelo bot, sem webhook e sem duplicar)
# → pergunta ao dono no canal de logs (só se a restauração foi pedida pelo /backup)
# → histórico comum autorizado: mensagens de membros e apps por webhook; mensagens próprias pela API do bot.
# O estado fica no banco. Cada envio grava checkpoint, então a retomada não repete nada.

FASES = ("limpeza", "estrutura", "proprias", "vitrines", "confirmacao", "membros")
STATUS_RESTORE_TERMINAIS = {"concluido", "concluido_com_avisos"}
_pedido_parar = False
_restore_task = None
_trava_restauracao = asyncio.Lock()
_trava_inicio_restore = asyncio.Lock()
_execucao_restore_id: str | None = None
_tarefas_limpeza_confirmacao: set[asyncio.Task] = set()


def estado_restauracao() -> dict:
    raw = store.get_config("restauracao")
    return json.loads(raw) if raw else {"status": "nenhuma"}


def resumo_restauracao() -> str:
    """Resumo legível do checkpoint, inclusive quando a restauração aguarda uma ação."""
    st = estado_restauracao()
    status = st.get("status", "nenhuma")
    if status == "nenhuma":
        return "Não há restauração registrada."
    linhas = [
        f"**Estado:** `{status}`",
        f"**Fase:** `{st.get('fase', 'desconhecida')}`",
        f"**Backup:** `{st.get('arquivo') or 'não informado'}`",
    ]
    if st.get("erro"):
        linhas.append(f"**Erro:** {str(st['erro'])[:700]}")
    if status == "concluido_com_avisos":
        linhas.append("**Resultado:** restauração concluída; alguns itens não puderam ser regenerados.")
        avisos = st.get("avisos") or []
        if avisos:
            linhas.append("**Itens pendentes:** " + "; ".join(map(str, avisos[:5]))[:800])
    elif status == "concluido":
        linhas.append("**Resultado:** restauração concluída.")
    if status == "aguardando":
        linhas.append("A restauração chegou à confirmação; falta escolher se o histórico comum será reproduzido.")
    return "\n".join(linhas)


def _salvar_restauracao(st: dict) -> None:
    # Uma tentativa retomada após uma falha passa a ter uma nova janela de
    # erro; isso permite ao coordenador do incidente distinguir erro antigo
    # de uma nova atividade suspeita ocorrida depois da pausa.
    if st.get("status") == "erro":
        st.setdefault("erro_em", time.time())
    elif st.get("status") == "rodando":
        st.pop("erro_em", None)
    store.set_config("restauracao", json.dumps(st, ensure_ascii=False))


def _checar_parada() -> None:
    from . import mensagens

    if _pedido_parar:
        raise mensagens.Pausado()


def _carregar_backup(nome: str) -> dict:
    caminho = PASTA / nome
    if not caminho.exists():
        raise RuntimeError("o arquivo do backup não existe mais")
    return _ler_backup(caminho)


def _posicoes_atuais(guild, dados: dict, mapa: dict) -> dict[str, int]:
    """Posição atual de cada canal do backup (por ID mapeado ou por nome). Usada só se o backup não tiver posição."""
    pares, _, _ = _parear_canais(dados.get("canais", []), guild)
    return {old: channel.position for old, channel in pares.items()}


async def _estrutura(guild, dados: dict, mapa: dict) -> tuple[list[str], list[str]]:
    """Servidor, cargos, canais, threads, cargos e convites. Cada etapa é independente."""
    capturados: list[str] = []

    class CapturarFalhas(logging.Handler):
        def emit(self, record):
            if record.levelno >= logging.WARNING:
                capturados.append(record.getMessage()[:300])

    captura = CapturarFalhas()
    log.addHandler(captura)
    feitos: list[str] = []
    roles_resolvidos: dict = {}
    canais_resolvidos: dict = {}

    async def etapa(nome: str, aguardando) -> list[str]:
        try:
            return await aguardando
        except Exception as exc:
            log.error("etapa %s falhou: %s", nome, exc, exc_info=True)
            return []

    try:
        feitos += await etapa("servidor", _servidor(guild, dados))
        feitos += await etapa("cargos", _cargos(guild, dados, mapa, roles_resolvidos))
        feitos += await etapa("canais", _canais(guild, dados, mapa, roles_resolvidos, canais_resolvidos))
        feitos += await etapa("configurações", _config_servidor(guild, dados.get("servidor", {}), mapa, canais_resolvidos))
        feitos += await etapa("emojis", _emojis(guild, dados, mapa, roles_resolvidos))
        feitos += await etapa("figurinhas", _figurinhas(guild, dados))
        feitos += await etapa("automod", _automod(guild, dados, mapa, roles_resolvidos, canais_resolvidos))
        feitos += await etapa("eventos", _eventos_programados(guild, dados, mapa, canais_resolvidos))
        feitos += await etapa("tópicos e threads", _threads(guild, dados, mapa, canais_resolvidos))
        feitos += await etapa("webhooks", _webhooks(guild, dados, mapa, canais_resolvidos))
        feitos += await etapa("banimentos", _banimentos(guild, dados))
        feitos += await etapa("cargos dos membros", _membros_cargos(guild, dados, mapa, roles_resolvidos))
        feitos += await etapa("convites", _convites(guild, dados, mapa, canais_resolvidos))
    finally:
        log.removeHandler(captura)
    return feitos, capturados


async def _fases(guild, dados: dict, st: dict) -> dict:
    from . import mensagens, shelves, incidente

    mapa: dict = st.setdefault("mapa", {})
    feitos: list = st.setdefault("feitos", [])
    avisos: list = st.setdefault("avisos", [])
    bot_id = context.bot().user.id
    snapshot_mensagens = dados.get("mensagens") or {}
    canais_snapshot_mensagens = snapshot_mensagens.get("canais") or {}
    tem_mensagens_reproduziveis = (
        isinstance(canais_snapshot_mensagens, dict)
        and any(
            isinstance(registros, list)
            and any(
                isinstance(registro, dict) and not mensagens._mensagem_controle(registro)
                for registro in registros
            )
            for registros in canais_snapshot_mensagens.values()
        )
    )
    st["tem_mensagens_backup"] = tem_mensagens_reproduziveis
    if not snapshot_mensagens.get("completo"):
        aviso = "snapshot de mensagens incompleto; conteúdo salvo será restaurado sob autorização e mensagens atuais serão preservadas"
        if aviso not in avisos:
            avisos.append(aviso)
    inicio = FASES.index(st["fase"]) if st.get("fase") in FASES else 0
    for fase in FASES[inicio:]:
        st["fase"] = fase
        _salvar_restauracao(st)
        log.info("restore: iniciando fase %s", fase)
        _checar_parada()
        if fase == "limpeza":
            # Limpezas retomadas refazem o mapa a partir do estado vivo; IDs de
            # uma tentativa anterior nunca são considerados correspondência.
            mapa.clear()
            _checar_parada()
            try:
                fc, erros_canais = await _limpar_canais_extras(guild, dados, mapa)
            except mensagens.Pausado:
                raise
            except Exception as exc:
                log.exception("pré-limpeza de canais falhou; continuando com o restante")
                fc, erros_canais = [], [f"pré-limpeza de canais falhou ({type(exc).__name__})"]
            feitos += fc
            avisos += erros_canais
            try:
                ft, erros_threads = await _limpar_threads_extras(guild, dados, mapa)
            except mensagens.Pausado:
                raise
            except Exception as exc:
                log.exception("pré-limpeza de tópicos falhou; continuando com o restante")
                ft, erros_threads = [], [f"pré-limpeza de tópicos falhou ({type(exc).__name__})"]
            feitos += ft
            avisos += erros_threads
            st["mapa"] = mapa
            falhas_mensagens = []
            if (dados.get("mensagens") or {}).get("completo"):
                try:
                    resultado_limpeza = await mensagens.limpar_extras_snapshot(
                        guild,
                        dados.get("mensagens", {}),
                        mapa,
                        checar=_checar_parada,
                    )
                    feitos.append(f"{resultado_limpeza['removidas']} mensagens excedentes removidas")
                    falhas_mensagens = resultado_limpeza["falhas"]
                    avisos += falhas_mensagens
                except mensagens.Pausado:
                    raise
                except Exception as exc:
                    log.exception("pré-limpeza de mensagens falhou; as demais fases continuarão")
                    falhas_mensagens = [f"pré-limpeza de mensagens falhou ({type(exc).__name__})"]
                    avisos += falhas_mensagens
            else:
                falhas_mensagens.append("snapshot sem histórico completo; mensagens excedentes preservadas")
                avisos += falhas_mensagens
            st["limpeza_concluida"] = not (erros_canais or erros_threads or falhas_mensagens)
            _salvar_restauracao(st)
        elif fase == "estrutura":
            try:
                f, capt = await _estrutura(guild, dados, mapa)
            except mensagens.Pausado:
                raise
            except Exception as exc:
                log.exception("reconciliação estrutural falhou; tentando concluir as fases restantes")
                f, capt = [], [f"etapa estrutural falhou ({type(exc).__name__})"]
            feitos += f
            avisos.extend(item for item in capt if item not in avisos)
            destinos_vitrines = _destinos_vitrines_backup(
                dados.get("mensagens", {}), mapa, bot_id
            )
            for tipo, canal_id in destinos_vitrines.items():
                # O banco pode ter sido apagado. Recupera do próprio snapshot
                # o canal onde cada painel estava publicado.
                store.set_config(f"{tipo.lower()}_channel_id", str(canal_id))
                log.info("vitrine %s remapeada para o canal %s pelo backup", tipo, canal_id)
            canal_pedido = str(st.get("canal_confirmacao") or "")
            if canal_pedido:
                st["canal_confirmacao"] = mapa.get(canal_pedido, canal_pedido)
            st["destinos_vitrines"] = destinos_vitrines
            # Canais recriados ganham o ID novo na configuração (inclusive o canal de logs).
            try:
                feitos += _remapear_config(mapa)
            except Exception as exc:
                log.exception("remapeamento de configurações falhou; continuando o restore")
                avisos.append(f"referências internas não foram totalmente remapeadas ({type(exc).__name__})")
            marcadores_falha = (
                "falha", "não restaur", "não remov", "indisponível",
                "não foi possível", "erro ao", "não consegui", "ambígu",
                "overwrite ignorado",
            )
            # Há avisos informativos durante o restore (por exemplo, threads
            # arquivadas que a API não deixa enumerar). Só falhas de etapa ou
            # operações explicitamente malsucedidas marcam estrutura parcial.
            falhas_registradas = [
                item for item in capt
                if item.startswith("etapa ")
                or item.startswith("pai do tópico ")
                or any(token in item.casefold() for token in marcadores_falha)
            ]
            falhas_estruturais = falhas_registradas + [
                item for item in f
                if any(token in item.casefold() for token in marcadores_falha)
            ]
            if falhas_estruturais:
                avisos.extend(item for item in falhas_estruturais if item not in avisos)
                st["erro"] = "A estrutura foi restaurada parcialmente; o restore continuará nas etapas recuperáveis."
                _salvar_restauracao(st)
                log.warning("restore: estrutura parcial; continuando as fases recuperáveis (%d falhas)", len(falhas_estruturais))
            elif capt:
                log.info("restore: %d avisos informativos na fase estrutural; continuando", len(capt))
        elif fase == "proprias":
            feitos.append("mensagens comuns, inclusive do bot, aguardam autorização explícita")
        elif fase == "vitrines":
            try:
                falhas_vitrines = await shelves.republicar_todas()
            except Exception as exc:
                log.exception("vitrines falharam; seguindo para confirmação e histórico")
                falhas_vitrines = [f"vitrines não conferidas ({type(exc).__name__})"]
            avisos.extend(item for item in falhas_vitrines if item not in avisos)
            if falhas_vitrines:
                feitos.append("vitrines processadas com itens pendentes")
            else:
                feitos.append("vitrines conferidas (sem duplicar)")
        elif fase == "confirmacao":
            if st.get("confirmar") and not st.get("respondido") and tem_mensagens_reproduziveis:
                if not st.get("confirmacao_enviada"):
                    try:
                        await _pedir_confirmacao(guild, st)
                        st["confirmacao_enviada"] = True
                        st["status"] = "aguardando"
                    except mensagens.Pausado:
                        raise
                    except Exception as exc:
                        log.exception("não foi possível pedir autorização das mensagens; encerrando sem replay")
                        avisos.append(
                            f"mensagens comuns não restauradas: confirmação indisponível ({type(exc).__name__})"
                        )
                        st["confirmar"] = False
                        st["membros_autorizados"] = False
                else:
                    st["status"] = "aguardando"
                if st.get("status") == "aguardando":
                    _salvar_restauracao(st)
                    return {"aguardando": True}
            if not st.get("membros_autorizados"):
                feitos.append("histórico comum preservado; não foi autorizado")
                break
        elif fase == "membros":
            try:
                r = await mensagens.restaurar_snapshot(
                    guild, snapshot_mensagens, mapa, fase="membros", bot_id=bot_id, checar=_checar_parada
                )
                feitos.append(f"{r['enviadas']} mensagens do histórico comum restauradas")
                avisos.extend(item for item in r["falhas"] if item not in avisos)
            except mensagens.Pausado:
                raise
            except Exception as exc:
                log.exception("replay de mensagens falhou; restore estrutural será finalizado")
                avisos.append(f"replay de mensagens interrompido ({type(exc).__name__})")
        log.info("restore: fase %s concluída", fase)
    st["status"] = "concluido_com_avisos" if avisos else "concluido"
    st.pop("erro", None)
    st["fase"] = "fim"
    _salvar_restauracao(st)
    log.info("restore: todas as fases recuperáveis encerradas; status=%s avisos=%d", st["status"], len(avisos))
    return {"feitos": feitos, "avisos": avisos}


def _destinos_vitrines_backup(snapshot_mensagens: dict, mapa: dict, bot_id: int) -> dict[str, str]:
    """Recupera a localização de vitrines pelo conteúdo salvo, sem consultar o DB antigo."""
    from . import shelves

    candidatos: dict[str, set[str]] = defaultdict(set)
    vitrines = catalogo.vitrines()
    custom_ids = {
        v.id: shelves._custom_ids(v.id)
        for v in vitrines
    }
    for canal_antigo, registros in (snapshot_mensagens.get("canais") or {}).items():
        canal_novo = mapa.get(str(canal_antigo))
        if not canal_novo:
            continue
        for registro in registros or []:
            if str((registro.get("autor") or {}).get("id")) != str(bot_id):
                continue
            componentes = registro.get("componentes", registro.get("components")) or []
            ids = shelves._custom_ids_da_mensagem(componentes)
            for vitrine in vitrines:
                if any(
                    cid in custom_ids[vitrine.id]
                    or shelves.vitrine_pelo_custom_id(cid) == vitrine.id
                    for cid in ids
                ):
                    candidatos[vitrine.id].add(str(canal_novo))
    destinos = {}
    for tipo, canais in candidatos.items():
        if len(canais) == 1:
            destinos[tipo] = next(iter(canais))
        else:
            log.warning(
                "vitrine %s aparece em mais de um canal do backup; destino não escolhido: %s",
                tipo, len(canais),
            )
    return destinos


async def _apagar_confirmacao(canal_id: int, mensagem_id: int) -> None:
    """Remove a pergunta já resolvida sem prender comandos ao rate limit."""
    try:
        await rest.request(
            "DELETE",
            f"/channels/{canal_id}/messages/{mensagem_id}",
            reason="Limpar confirmação de restauração encerrada",
        )
    except rest.DiscordHTTPError as exc:
        if exc.status == 404:
            _remover_confirmacao_pendente(canal_id, mensagem_id)
        else:
            log.warning("não consegui apagar a confirmação antiga do restore: %s", exc)
    except rest.DiscordTransportError as exc:
        log.warning("falha de rede ao apagar confirmação antiga do restore: %s", exc)
    else:
        _remover_confirmacao_pendente(canal_id, mensagem_id)


def _confirmacoes_pendentes() -> list[tuple[int, int]]:
    try:
        valores = json.loads(store.get_config("restauracao_confirmacoes_pendentes") or "[]")
    except (TypeError, ValueError):
        log.exception("fila persistida de confirmações inválida")
        return []
    resultado = []
    for valor in valores if isinstance(valores, list) else []:
        try:
            resultado.append((int(valor[0]), int(valor[1])))
        except (TypeError, ValueError, IndexError):
            continue
    return resultado


def _salvar_confirmacoes_pendentes(valores: list[tuple[int, int]]) -> None:
    store.set_config("restauracao_confirmacoes_pendentes", json.dumps(valores))


def _registrar_confirmacao_pendente(canal_id: int, mensagem_id: int) -> None:
    fila = _confirmacoes_pendentes()
    item = (canal_id, mensagem_id)
    if item not in fila:
        fila.append(item)
        _salvar_confirmacoes_pendentes(fila)


def _remover_confirmacao_pendente(canal_id: int, mensagem_id: int) -> None:
    fila = [p for p in _confirmacoes_pendentes() if p != (canal_id, mensagem_id)]
    _salvar_confirmacoes_pendentes(fila)


async def _limpar_confirmacao(st: dict) -> None:
    """Agenda a remoção da pergunta; parar/retomar nunca espera por uma chamada REST."""
    canal_id = st.get("confirmacao_canal_id")
    mensagem_id = st.get("confirmacao_mensagem_id")
    if canal_id and mensagem_id:
        try:
            canal_id, mensagem_id = int(canal_id), int(mensagem_id)
        except (TypeError, ValueError):
            log.warning("IDs inválidos no checkpoint da confirmação; limpeza não executada")
        else:
            _registrar_confirmacao_pendente(canal_id, mensagem_id)
            task = asyncio.create_task(_apagar_confirmacao(canal_id, mensagem_id))
            _tarefas_limpeza_confirmacao.add(task)
            task.add_done_callback(_tarefas_limpeza_confirmacao.discard)
    st.pop("confirmacao_canal_id", None)
    st.pop("confirmacao_mensagem_id", None)
    st["confirmacao_enviada"] = False


async def _pedir_confirmacao(guild, st: dict) -> None:
    """Pergunta onde houver um canal de saída disponível; prefere logs e usa o canal do comando como fallback."""
    from ..ui.emojis import E
    from . import rest

    ids = [
        store.ajuste("logs_channel_id"),
        str(settings.LOGS_CHANNEL_ID or ""),
        str(st.get("canal_confirmacao") or ""),
        str(guild.system_channel.id) if guild.system_channel else "",
    ]
    destinos = []
    vistos = set()
    for valor in ids:
        if not valor or not str(valor).isdigit() or int(valor) in vistos:
            continue
        vistos.add(int(valor))
        canal = guild.get_channel_or_thread(int(valor))
        if canal is not None:
            destinos.append(canal)
    destinos.extend(
        canal for canal in guild.text_channels
        if canal.id not in vistos
    )
    if not destinos:
        raise RuntimeError("não há canal disponível para pedir autorização do histórico")
    payload = {
        "content": (
            f"<@{settings.ADMIN_ID}> {E.alert} **Confirmação de histórico de mensagens**\n"
            "A etapa estrutural e as vitrines foram processadas. Mensagens comuns, inclusive as do próprio bot, "
            "só serão reproduzidas se você autorizar.\n"
            "Deseja restaurar o histórico comum de membros, bots e webhooks?"
        ),
        "components": [
            {
                "type": 1,
                "components": [
                    {"type": 2, "style": 3, "label": "Sim, restaurar", "custom_id": "rst:sim"},
                    {"type": 2, "style": 4, "label": "Não restaurar", "custom_id": "rst:nao"},
                ],
            }
        ],
    }
    erros = []
    for canal in destinos:
        try:
            mensagem = await rest.send_message(canal.id, payload)
            if isinstance(mensagem, dict) and mensagem.get("id"):
                st["confirmacao_canal_id"] = str(canal.id)
                st["confirmacao_mensagem_id"] = str(mensagem["id"])
            log.info("confirmação de histórico enviada no canal %s", canal.id)
            return
        except (rest.DiscordHTTPError, rest.DiscordTransportError) as exc:
            erros.append(f"{canal.id}:{type(exc).__name__}")
            log.warning("canal %s não recebeu confirmação de histórico: %s", canal.id, exc)
    raise RuntimeError(
        "não foi possível enviar a confirmação em nenhum canal disponível ("
        + ", ".join(erros[:5]) + ")"
    )


async def _executar(guild, dados: dict, st: dict) -> dict:
    global _ocupado, _pedido_parar, _execucao_restore_id
    from . import mensagens

    async with _trava_restauracao:
        # Identifica uma geração da restauração antes de aguardar a trava.
        # Depois de obtê-la, uma tarefa antiga não pode executar fases usando
        # o estado "rodando" de uma tarefa nova.
        execucao_id = st.get("execucao_id")
        atual = estado_restauracao()
        if (
            not execucao_id
            or atual.get("status") != "rodando"
            or atual.get("execucao_id") != execucao_id
        ):
            log.warning("execução de restore obsoleta descartada antes de iniciar as fases")
            return {"ignorada": True}
        if _execucao_restore_id is not None:
            # A trava de restauração garante exclusão mútua. Um marcador não nulo
            # após adquiri-la é estado órfão em memória, não uma execução ativa.
            log.error("marcador órfão de execução de restore; recuperando antes de continuar")
            _execucao_restore_id = None
        _execucao_restore_id = execucao_id
        _ocupado = True
        try:
            try:
                resultado = await _fases(guild, dados, st)
            except asyncio.CancelledError:
                st["status"] = "parado"
                st["erro"] = "Restore pausado pelo dono; a próxima retomada refaz o pareamento pelo estado atual."
                st["pausado_em"] = time.time()
                _salvar_restauracao(st)
                log.warning("restore pausado na fase %s; seguro para retomar por reconciliação", st.get("fase"))
                raise
            except mensagens.Pausado:
                st["status"] = "parado"
                st["pausado_em"] = time.time()
                st["erro"] = "Restauração interrompida pelo dono; retomada refaz o pareamento pelo estado atual."
                _salvar_restauracao(st)
                log.warning("restauração parada pelo dono na fase %s", st.get("fase"))
                return {"pausado": True}
            except Exception as exc:
                log.exception("restauração interrompida")
                st["status"] = "erro"
                st["erro"] = str(exc)[:300]
                _salvar_restauracao(st)
                resultado = {
                    "erro": "Restauração interrompida; use /restauracao continuar após corrigir."
                }
        finally:
            _ocupado = False
            _pedido_parar = False
            # O marcador protege somente a execução que está dentro da trava.
            # Mantê-lo após sucesso/erro bloqueava confirmação, retomada e todo
            # restore seguinte durante a vida do processo.
            if _execucao_restore_id == execucao_id:
                _execucao_restore_id = None
        if resultado.get("aguardando"):
            return resultado
        concluida = st.get("status") in STATUS_RESTORE_TERMINAIS
        com_avisos = st.get("status") == "concluido_com_avisos"
        await notify.registrar(
            "backup_restaurado" if concluida else "backup_restauracao_incompleta",
            (
                f"{E.reload} Restauração concluída com avisos"
                if com_avisos
                else f"{E.reload} Restauração concluída"
                if concluida
                else f"{E.alert} Restauração incompleta"
            ),
            CORES["aviso"] if com_avisos and "aviso" in CORES else CORES["info"] if concluida else CORES["erro"],
            [
                ("Motivo", st.get("motivo", "manual")),
                ("Backup", st.get("arquivo", "?")),
                ("Status", "concluída com avisos; não será repetida automaticamente" if com_avisos else "concluída" if concluida else "interrompida; requer retomada"),
                ("Feito", "; ".join(resultado.get("feitos", []))[:900] or "nada precisava mudar"),
                (
                    "Falhas/avisos",
                    ("; ".join(resultado.get("avisos", []))[:700]
                     or str(resultado.get("erro") or st.get("erro") or "nenhum"))[:900],
                ),
            ],
        )
        return resultado


def _iniciar_tarefa(guild, dados: dict, st: dict) -> None:
    global _restore_task
    _restore_task = asyncio.create_task(_executar(guild, dados, st))


async def restaurar(
    guild: discord.Guild,
    dados: dict | None = None,
    motivo: str = "manual",
    arquivo: str | None = None,
    confirmar: bool = False,
    canal_confirmacao: int | None = None,
) -> dict:
    """Restauração completa. confirmar=True pede autorização antes de reproduzir mensagens comuns."""
    global _ocupado, _pedido_parar, _restore_task
    selecionado = ultimo_valido() if dados is None else None
    dados = dados if dados is not None else (selecionado[1] if selecionado else None)
    if dados is None:
        return {"erro": "Não há backup para restaurar."}
    if not isinstance(dados, dict):
        return {"erro": "O arquivo de backup não contém um objeto válido."}
    if dados.get("guild_id") and dados["guild_id"] != str(guild.id):
        return {"erro": "Este backup pertence a outro servidor."}
    try:
        schema = int(dados.get("schema_version", 1) or 1)
    except (TypeError, ValueError):
        return {"erro": "A versão do formato do backup é inválida."}
    if schema < 2:
        return {"erro": "Este formato de backup é antigo demais e não contém estrutura compatível para uma restauração segura."}
    if (
        not isinstance(dados.get("canais"), list)
        or not isinstance(dados.get("cargos"), list)
        or not isinstance(dados.get("servidor"), dict)
    ):
        return {"erro": "O backup não contém a estrutura mínima para restaurar com segurança."}
    from . import incidente
    async with _trava_inicio_restore:
        if _trava_backup.locked():
            return {"erro": "Um backup está sendo gravado; a restauração aguardará o fim da gravação para não ler um snapshot parcial."}
        anterior = _restore_task
        if anterior is not None and not anterior.done():
            if anterior is asyncio.current_task():
                return {"erro": "A restauração atual não pode substituir a si própria."}
            antigo = estado_restauracao()
            log.warning(
                "nova restauração substituindo execução anterior: arquivo=%s fase=%s motivo=%s",
                antigo.get("arquivo"), antigo.get("fase"), motivo,
            )
            _pedido_parar = True
            anterior.cancel()
            await asyncio.gather(anterior, return_exceptions=True)
        checkpoint_anterior = estado_restauracao()
        await _limpar_confirmacao(checkpoint_anterior)
        if _ocupado:
            # Com a tarefa anterior aguardada, esta trava serve como barreira
            # final para drenar qualquer fase que ainda esteja liberando recursos.
            async with _trava_restauracao:
                pass
            if _ocupado:
                log.error("flag _ocupado órfã após drenar a trava; limpando estado local")
                _ocupado = False

        # Checkpoints antigos em erro/parado/aguardando não bloqueiam uma nova
        # solicitação. Ela começa pela limpeza e reconcilia o estado atual.
        _pedido_parar = False
        st = {
            "execucao_id": uuid.uuid4().hex,
            "status": "rodando",
            "fase": "limpeza",
            "arquivo": arquivo or (selecionado[0].name if selecionado else None),
            "motivo": motivo,
            "confirmar": confirmar,
            "canal_confirmacao": str(canal_confirmacao or ""),
            "limpeza_concluida": False,
            "confirmacao_enviada": False,
            "mapa": {},
            "feitos": [],
            "avisos": [],
        }
        _salvar_restauracao(st)
        _iniciar_tarefa(guild, dados, st)
        tarefa_nova = _restore_task
    if confirmar:
        return {"iniciada": True}
    try:
        return await asyncio.shield(tarefa_nova)
    except asyncio.CancelledError:
        if tarefa_nova.cancelled():
            return {"pausado": True, "feitos": [], "avisos": []}
        raise


async def _parar_exclusivo() -> str:
    global _pedido_parar
    st = estado_restauracao()
    await _limpar_confirmacao(st)
    if st.get("status") in STATUS_RESTORE_TERMINAIS:
        return "A restauração já terminou; não há operações para parar."
    tarefa = _restore_task
    if tarefa is not None and not tarefa.done() and tarefa is not asyncio.current_task():
        _pedido_parar = True
        tarefa.cancel()
        await asyncio.gather(tarefa, return_exceptions=True)
        st = estado_restauracao()
        if st.get("status") not in STATUS_RESTORE_TERMINAIS | {"nenhuma"}:
            st["status"] = "parado"
            st["erro"] = "Restauração interrompida pelo dono; retomada reconcilia o estado atual."
            st["pausado_em"] = time.time()
        if st.get("status") not in STATUS_RESTORE_TERMINAIS | {"nenhuma"}:
            _salvar_restauracao(st)
        return "Restauração parada. Use `/restauracao continuar` para retomar ou inicie uma restauração nova."
    if st.get("status") == "rodando":
        st["status"] = "parado"
        st["erro"] = "Checkpoint ativo sem tarefa local; pausado para evitar operações concorrentes."
        st["pausado_em"] = time.time()
        _salvar_restauracao(st)
        return "Checkpoint pausado; nenhuma etapa seguirá em segundo plano. Use `/restauracao continuar` para retomar."
    if st.get("status") == "aguardando":
        st["status"] = "parado"
        st["pausado_em"] = time.time()
        _salvar_restauracao(st)
        return "Restauração pausada; use `/restauracao continuar` para retomar."
    if st.get("status") == "parado":
        return "A restauração já está pausada. Use `/restauracao continuar` ou escolha outro backup em `/backup` para substituir o checkpoint."
    if st.get("status") == "erro":
        return "A restauração não está executando: ficou em erro. Use `/restauracao continuar` ou escolha outro backup em `/backup` para substituir o checkpoint."
    return "Não há restauração em andamento."


async def parar() -> str:
    async with _trava_inicio_restore:
        return await _parar_exclusivo()


async def _continuar_exclusivo(guild) -> str:
    global _pedido_parar
    st = estado_restauracao()
    tarefa = _restore_task
    if tarefa is not None and not tarefa.done() and tarefa is not asyncio.current_task():
        _pedido_parar = True
        tarefa.cancel()
        await asyncio.gather(tarefa, return_exceptions=True)
        st = estado_restauracao()
    if st.get("status") == "rodando":
        st["status"] = "parado"
        st["pausado_em"] = time.time()
        _salvar_restauracao(st)
    if st.get("status") == "aguardando":
        st["status"] = "parado"
        st["pausado_em"] = time.time()
        await _limpar_confirmacao(st)
        _salvar_restauracao(st)
    if st.get("status") not in ("parado", "erro"):
        return "Não há checkpoint de restauração para continuar."
    if _ocupado:
        async with _trava_restauracao:
            pass
    try:
        dados = _carregar_backup(st["arquivo"])
    except (OSError, ValueError, KeyError, RuntimeError) as exc:
        st["status"] = "erro"
        st["erro"] = f"backup pendente ilegível: {exc}"[:300]
        _salvar_restauracao(st)
        log.exception("não foi possível retomar o backup %s", st.get("arquivo"))
        return "O backup da restauração pendente está ilegível; a proteção permanece ativa."
    st["status"] = "rodando"
    st.pop("pausado_em", None)
    st.pop("erro_em", None)
    await _limpar_confirmacao(st)
    if st.get("fase") not in FASES:
        st["fase"] = "limpeza"
    st["execucao_id"] = uuid.uuid4().hex
    _salvar_restauracao(st)
    _pedido_parar = False
    _iniciar_tarefa(guild, dados, st)
    return f"Continuando a restauração a partir da fase {st.get('fase')}."


async def continuar(guild) -> str:
    async with _trava_inicio_restore:
        return await _continuar_exclusivo(guild)


async def _responder_confirmacao_exclusivo(guild, sim: bool) -> str:
    st = estado_restauracao()
    tarefa = _restore_task
    if _ocupado and tarefa is not None and not tarefa.done() and tarefa is not asyncio.current_task():
        await asyncio.gather(tarefa, return_exceptions=True)
        st = estado_restauracao()
    if st.get("status") != "aguardando":
        return "Não há pedido de confirmação pendente."
    try:
        dados = _carregar_backup(st["arquivo"])
    except (OSError, ValueError, KeyError, RuntimeError) as exc:
        st["status"] = "erro"
        st["erro"] = f"backup pendente ilegível: {exc}"[:300]
        _salvar_restauracao(st)
        log.exception("backup da confirmação pendente está ilegível")
        return "O backup está ilegível; a proteção permanece ativa."
    st["respondido"] = True
    st["membros_autorizados"] = bool(sim)
    st["confirmar"] = False
    if st.get("fase") not in FASES:
        st["fase"] = "confirmacao"
    st["status"] = "rodando"
    st.pop("pausado_em", None)
    st.pop("erro_em", None)
    st["execucao_id"] = uuid.uuid4().hex
    _salvar_restauracao(st)
    _iniciar_tarefa(guild, dados, st)
    return "Restaurando o histórico comum." if sim else "O histórico comum não será reproduzido."


async def responder_confirmacao(guild, sim: bool) -> str:
    async with _trava_inicio_restore:
        return await _responder_confirmacao_exclusivo(guild, sim)


async def _retomar_pendente_exclusivo(guild) -> None:
    """Na inicialização: retoma sozinha só o que estava rodando (queda). O que o dono parou não volta sozinho."""
    st = estado_restauracao()
    if guild is None:
        return
    if _restore_task is not None and not _restore_task.done():
        log.info("retomada de inicialização ignorada: já existe uma tarefa local de restauração")
        return
    if st.get("status") == "rodando" and not _ocupado and (_restore_task is None or _restore_task.done()):
        if st.get("fase") not in FASES:
            st["fase"] = "limpeza"
            _salvar_restauracao(st)
        log.warning("retomando restauração interrompida na fase %s", st.get("fase"))
        try:
            _iniciar_tarefa(guild, _carregar_backup(st["arquivo"]), st)
        except (OSError, ValueError, KeyError, RuntimeError) as exc:
            st["status"] = "erro"
            st["erro"] = f"backup pendente ilegível: {exc}"[:300]
            _salvar_restauracao(st)
            log.exception("não foi possível retomar restauração interrompida")
    elif st.get("status") == "aguardando":
        log.info("confirmação de restore pendente; aguardando ação do dono")


async def retomar_pendente(guild) -> None:
    """Serializa a retomada de inicialização com parar, continuar e nova restauração."""
    async with _trava_inicio_restore:
        await _retomar_pendente_exclusivo(guild)


def agendar_restauracao(guild: discord.Guild) -> None:
    """A contenção coordena silêncio, punições e restauração; não usa timer cego."""
    from . import incidente

    asyncio.create_task(incidente.iniciar(guild, "Alterações em massa"))


# ── Diário ────────────────────────────────────────────────────────────────────


def _precisa_backup() -> bool:
    caminho = PASTA / NOME_BACKUP_DIARIO
    if not caminho.exists():
        return True
    try:
        # O vencimento vem do instante persistido no manifesto, não do tempo
        # que este processo ficou ligado nem do relógio monotônico da sessão.
        with caminho.open("r", encoding="utf-8") as arquivo:
            manifesto = json.load(arquivo)
        criado_em = manifesto.get("criado_em")
        if criado_em:
            instante = datetime.fromisoformat(criado_em)
            if instante.tzinfo is None:
                instante = instante.astimezone()
            agora = datetime.now().astimezone()
            return (agora - instante.astimezone()).total_seconds() >= 24 * 3600
        # Compatibilidade com manifestos antigos sem timestamp no conteúdo.
        return time.time() - caminho.stat().st_mtime >= 24 * 3600
    except (OSError, ValueError, TypeError, AttributeError):
        log.exception("não consegui consultar o horário do backup diário")
        return True


async def _loop() -> None:
    while True:
        try:
            guild = (
                context.bot().get_guild(settings.GUILD_ID)
                if settings.GUILD_ID
                else None
            )
            if guild is not None and _precisa_backup():
                await criar(guild, motivo="diário")
            if guild is not None and not em_raid():
                from . import mensagens

                await mensagens.atualizar_avatares(guild)
                await mensagens.arquivar(guild)
            if guild is not None:
                from . import raid

                await raid.renovar_timeouts(guild)
            from . import config_backup

            config_backup.diario()
        except Exception:
            log.exception("falha no backup diário")
        await asyncio.sleep(INTERVALO_S)


_loop_task = None
_restore_task = None


def iniciar() -> None:
    global _loop_task
    for canal_id, mensagem_id in _confirmacoes_pendentes():
        task = asyncio.create_task(_apagar_confirmacao(canal_id, mensagem_id))
        _tarefas_limpeza_confirmacao.add(task)
        task.add_done_callback(_tarefas_limpeza_confirmacao.discard)
    if _loop_task is None or _loop_task.done():
        _loop_task = asyncio.create_task(_loop())


async def close():
    for task in (*(_tarefas_limpeza_confirmacao), _loop_task, _restore_task):
        if task is not None:
            task.cancel()
    await asyncio.gather(
        *(t for t in (*_tarefas_limpeza_confirmacao, _loop_task, _restore_task) if t is not None),
        return_exceptions=True,
    )
