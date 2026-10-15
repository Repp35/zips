"""Inspeção conservadora após reconexão; divergência isolada não constitui raid."""
import asyncio
import logging
from collections import Counter, defaultdict
from datetime import timedelta

import discord

from ..config import settings
from . import backup, incidente, raid, rest, reconciliar, mensagens

log = logging.getLogger(__name__)
_monitor_task = None


async def verificar_estrutura(guild) -> bool:
    """Checagem leve e periódica: só cache do gateway + snapshot em cache, sem REST."""
    if not raid.ligado() or incidente.ativo(guild.id):
        return False
    estado = backup.estado_restauracao()
    if estado.get("status") == "rodando":
        return False
    dados = backup.ultimo_para_inspecao(guild.id)
    if not dados:
        return False

    desejados = dados.get("canais", [])
    pares, _, extras = backup._parear_canais(desejados, guild)
    faltando = max(0, len(desejados) - len(pares))
    duplicados = Counter(
        (getattr(c, "name", "").casefold(), type(c).__name__) for c in extras
    )
    maior_grupo = max(duplicados.values(), default=0)
    devastacao = (
        len(desejados) >= settings.RAID_OFFLINE_DEVASTACAO_MIN_CANAIS
        and faltando >= max(
            settings.RAID_OFFLINE_DEVASTACAO_AUSENTES_MIN,
            (len(desejados) * settings.RAID_OFFLINE_DEVASTACAO_PERCENTUAL + 99) // 100,
        )
    )
    grave = (
        len(desejados) >= settings.RAID_OFFLINE_BACKUP_MIN_CANAIS
        and faltando >= max(
            settings.RAID_OFFLINE_AUSENTES_GRAVE_MIN,
            (len(desejados) * settings.RAID_OFFLINE_AUSENTES_GRAVE_PERCENTUAL + 99) // 100,
        )
    )
    roles_pares, _, roles_extras = backup._parear_cargos(dados.get("cargos", []), guild)
    roles_faltando = len(dados.get("cargos", [])) - len(roles_pares)
    cargos_por_backup = {r["id"]: r for r in dados.get("cargos", [])}
    roles_alterados = sum(
        role.name != cargos_por_backup[old]["nome"]
        or role.permissions.value != cargos_por_backup[old]["permissoes"]
        for old, role in roles_pares.items()
        if old in cargos_por_backup
    )

    def norm(value):
        return " ".join(str(value or "").casefold().split())

    parent_map = {old: str(channel.id) for old, channel in pares.items()}
    esperados_threads = Counter(
        (parent_map.get(t["pai"], t["pai"]), norm(t["nome"]), bool(t["privado"]))
        for t in dados.get("threads", [])
    )
    atuais_threads = Counter(
        (str(t.parent_id), norm(t.name), t.type == discord.ChannelType.private_thread)
        for t in guild.threads
    )
    extras_threads = sum(
        max(0, n - esperados_threads[chave])
        for chave, n in atuais_threads.items()
    )
    topicos_duplicados = max(0, max(
        (n - esperados_threads[chave] for chave, n in atuais_threads.items()),
        default=0,
    ))
    suspeito = (
        devastacao
        or len(extras) >= settings.RAID_OFFLINE_CANAIS_EXTRAS_SINAL
        or maior_grupo >= settings.RAID_CANAIS_DUPLICADOS_MIN
        or topicos_duplicados >= settings.RAID_TOPICOS_DUPLICADOS_MIN
        or extras_threads >= settings.RAID_TOPICOS_DUPLICADOS_MIN
        or roles_faltando >= settings.RAID_OFFLINE_CARGOS_AUSENTES_SINAL
        or roles_alterados >= settings.RAID_OFFLINE_CARGOS_ALTERADOS_SINAL
        or len(roles_extras) >= settings.RAID_OFFLINE_CARGOS_AUSENTES_SINAL
        or (grave and len(extras) >= 1)
    )
    if not suspeito:
        return False
    log.warning(
        "verificação estrutural periódica encontrou divergência: ausentes=%d extras=%d duplicados=%d tópicos_extras=%d cargos_ausentes=%d cargos_alterados=%d cargos_extras=%d",
        faltando, len(extras), maior_grupo, extras_threads, roles_faltando, roles_alterados, len(roles_extras),
    )
    await incidente.iniciar(guild, "Dano estrutural detectado na verificação periódica")
    return True


async def _monitor(guild):
    # A inspeção completa roda uma vez em background. A cada intervalo, a
    # checagem estrutural é local e leve; eventos do gateway cobrem as ações ao vivo.
    try:
        await verificar(guild)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("inspeção inicial após reconexão falhou")
    while True:
        await asyncio.sleep(settings.RAID_INSPECAO_INTERVALO_S)
        try:
            await verificar_estrutura(guild)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("verificação estrutural periódica falhou")


def iniciar(guild) -> None:
    global _monitor_task
    if _monitor_task is None or _monitor_task.done():
        _monitor_task = asyncio.create_task(
            _monitor(guild), name=f"inspecao-raid-{guild.id}"
        )


async def fechar() -> None:
    global _monitor_task
    if _monitor_task is not None:
        _monitor_task.cancel()
        await asyncio.gather(_monitor_task, return_exceptions=True)
        _monitor_task = None


async def verificar(guild):
    # Esta verificação é deliberadamente independente dos contadores em tempo real:
    # ao reconectar, o bot precisa procurar sinais deixados enquanto estava offline.
    if not raid.ligado():
        log.info("inspeção offline não executada: anti-raid está desligado")
        return False
    st = backup.estado_restauracao()
    # Só evitamos uma inspeção paralela enquanto a estrutura está sendo alterada.
    # A confirmação de mensagens comuns não pode bloquear a inspeção de segurança.
    if st.get("status") == "rodando":
        log.info("inspeção offline adiada: restore estrutural ainda está rodando")
        return False
    dados = backup.ultimo_para_inspecao(guild.id)
    if not dados:
        log.warning("inspeção offline sem snapshot estrutural válido para a guild")
        return False
    desejados = dados.get("canais", [])
    pares, ambiguos, extras = backup._parear_canais(desejados, guild)
    faltando = len(desejados) - len(pares)
    nomes = Counter((c.name, type(c).__name__) for c in extras)
    duplicados = max(nomes.values(), default=0)
    cargos_pares, _, cargos_extras = backup._parear_cargos(dados.get("cargos", []), guild)
    cargos_faltando = len(dados.get("cargos", [])) - len(cargos_pares)
    cargos_por_backup = {c["id"]: c for c in dados.get("cargos", [])}
    cargos_alterados = sum(
        (role.name != cargos_por_backup[old]["nome"]
         or role.permissions.value != cargos_por_backup[old]["permissoes"]
         or role.position != cargos_por_backup[old].get("posicao", role.position))
        for old, role in cargos_pares.items()
        if old in cargos_por_backup
    )
    canais_por_backup = {c["id"]: c for c in desejados}
    categorias_por_id = {
        c["id"]: c["nome"] for c in desejados if c["tipo"] == "categoria"
    }
    canais_alterados = 0
    for old, canal in pares.items():
        salvo = canais_por_backup.get(old)
        if not salvo:
            continue
        categoria_salva = categorias_por_id.get(salvo.get("pai"))
        overwrites_salvos = {}
        for ow in salvo.get("permissoes", []):
            target = (cargos_pares.get(ow["id"]) if ow["tipo"] == "cargo"
                      else guild.get_member(int(ow["id"])))
            if target is not None:
                overwrites_salvos[target] = (ow["allow"], ow["deny"])
        overwrites_atuais = {}
        for target, overwrite in canal.overwrites.items():
            allow, deny = overwrite.pair()
            overwrites_atuais[target] = (allow.value, deny.value)
        if (canal.name != salvo["nome"]
                or getattr(canal, "topic", None) != salvo.get("topico")
                or canal.position != salvo.get("posicao", canal.position)
                or (canal.category.name if canal.category else None) != categoria_salva
                or overwrites_atuais != overwrites_salvos):
            canais_alterados += 1
    servidor = dados.get("servidor", {})
    config_alterada = any((
        guild.name != servidor.get("nome", guild.name),
        (guild.description or "") != (servidor.get("descricao") or ""),
        guild.default_role.permissions.value != servidor.get("everyone", guild.default_role.permissions.value),
        guild.verification_level.value != servidor.get("verification_level", guild.verification_level.value),
        guild.default_notifications.value != servidor.get("default_notifications", guild.default_notifications.value),
        guild.explicit_content_filter.value != servidor.get("explicit_content_filter", guild.explicit_content_filter.value),
    ))
    ausentes_grave_pct = settings.RAID_OFFLINE_AUSENTES_GRAVE_PERCENTUAL
    grave = (
        len(desejados) >= settings.RAID_OFFLINE_BACKUP_MIN_CANAIS
        and faltando >= max(
            settings.RAID_OFFLINE_AUSENTES_GRAVE_MIN,
            (len(desejados) * ausentes_grave_pct + 99) // 100,
        )
    )

    devastacao = (
        len(desejados) >= settings.RAID_OFFLINE_DEVASTACAO_MIN_CANAIS
        and faltando >= max(
            settings.RAID_OFFLINE_DEVASTACAO_AUSENTES_MIN,
            (len(desejados) * settings.RAID_OFFLINE_DEVASTACAO_PERCENTUAL + 99) // 100,
        )
    )
    sinal_estrutural_forte = (
        duplicados >= settings.RAID_CANAIS_DUPLICADOS_MIN
        or len(extras) >= settings.RAID_OFFLINE_CANAIS_EXTRAS_SINAL
        or devastacao
    )
    if sinal_estrutural_forte and not incidente.ativo(guild.id):
        log.warning(
            "contenção offline iniciada antes de consultar históricos: extras=%d, duplicados=%d, ausentes=%d",
            len(extras), duplicados, faltando,
        )
        await incidente.iniciar(guild, "Estrutura suspeita encontrada após reconexão")

    threads = list(guild.threads)
    # Se a estrutura já confirma uma raid, não atrase contenção consultando
    # arquivos de centenas de canais. Os tópicos ativos continuam analisados.
    canais_topicos = list(guild.channels)[:settings.RAID_SCAN_CANAIS_OFFLINE_MAX]
    for canal in (() if sinal_estrutural_forte else canais_topicos):
        if not hasattr(canal, "archived_threads"):
            continue
        for privado in ((False,) if isinstance(canal, discord.ForumChannel) else (False, True)):
            try:
                async for thread in canal.archived_threads(
                    limit=100,
                    **({} if isinstance(canal, discord.ForumChannel) else {"private": privado}),
                ):
                    threads.append(thread)
            except discord.HTTPException as exc:
                log.warning("tópicos arquivados de %s indisponíveis na inspeção offline: %s", canal.id, exc)
    topicos = Counter((t.parent_id, t.name) for t in threads)
    topicos_repetidos = max(topicos.values(), default=0) >= settings.RAID_TOPICOS_DUPLICADOS_MIN
    mensagens_repetidas = False
    autores_spam = set()
    historico_sumiu = False
    por_texto = defaultdict(lambda: [0, set()])
    canais_esvaziados = 0
    snapshot_msgs = dados.get("mensagens", {}).get("canais", {})
    snapshot_mensagens_completo = bool(dados.get("mensagens", {}).get("completo"))
    mapa_atual = {ch.id: antigo for antigo, ch in pares.items()}
    sem = asyncio.Semaphore(4)

    async def amostrar(ch):
        async with sem:
            try:
                limite = settings.RAID_AMOSTRA_MENSAGENS_OFFLINE
                return ch, await rest.request("GET", f"/channels/{ch.id}/messages?limit={limite}") or []
            except (rest.DiscordHTTPError, rest.DiscordTransportError) as exc:
                log.warning("amostragem offline do canal %s indisponível: %s", ch.id, exc)
                return ch, None

    canais_texto = list(guild.text_channels)
    if sinal_estrutural_forte:
        ids_extras = {c.id for c in extras}
        canais_texto.sort(key=lambda c: (
            c.id not in ids_extras,
            -getattr(getattr(c, "created_at", None), "timestamp", lambda: 0)(),
        ))
        canais_texto = canais_texto[:settings.RAID_SCAN_CANAIS_OFFLINE_MAX]
    else:
        canais_texto = canais_texto[:settings.RAID_SCAN_CANAIS_OFFLINE_MAX]

    for inicio in range(0, len(canais_texto), 32):
        lotes = await asyncio.gather(*(amostrar(ch) for ch in canais_texto[inicio:inicio + 32]))
        for ch, lote in lotes:
            if lote is None:
                continue
            antigos = snapshot_msgs.get(mapa_atual.get(ch.id, ""), [])
            # Uma página cheia pode esconder mensagens antigas; só conclui que o canal esvaziou
            # quando a API confirma que o histórico atual tem menos de 50 mensagens.
            limite_amostra = settings.RAID_AMOSTRA_MENSAGENS_OFFLINE
            if (snapshot_mensagens_completo
                    and settings.RAID_HISTORICO_SALVO_MIN <= len(antigos) <= settings.RAID_HISTORICO_SALVO_MAX
                    and len(lote) < limite_amostra):
                ausentes, _ = reconciliar.atribuir_mensagens(
                    antigos, lote, reconciliar.assinatura_mensagem,
                    mensagens._assinatura_atual)
                restantes = len(antigos) - len(ausentes)
                if restantes <= len(antigos) // 5:
                    canais_esvaziados += 1
            for msg in lote:
                texto = " ".join((msg.get("content") or "").casefold().split())
                if len(texto) < settings.RAID_MENSAGEM_MIN_CARACTERES:
                    continue
                autor = (msg.get("author") or {}).get("id")
                webhook_id = msg.get("webhook_id")
                metadata = msg.get("interaction_metadata") or msg.get("interaction") or {}
                executor = (metadata.get("user") or {}).get("id") if isinstance(metadata, dict) else None
                tipo = "webhook" if webhook_id else "aplicativo" if executor else "bot" if (msg.get("author") or {}).get("bot") else "membro"
                alvo = webhook_id or executor or autor
                item = por_texto[(texto[:500], alvo, webhook_id, tipo)]
                item[0] += 1
                item[1].add(ch.id)
    autores_spam = {
        (alvo, webhook_id, tipo)
        for (_, alvo, webhook_id, tipo), (n, canais) in por_texto.items()
        if (n >= settings.RAID_SPAM_OFFLINE_MENSAGENS_MIN
            and len(canais) >= settings.RAID_SPAM_OFFLINE_CANAIS_MIN)
        or n >= settings.RAID_SPAM_OFFLINE_AUTOR_MIN
    }
    mensagens_repetidas = bool(autores_spam)
    historico_sumiu = canais_esvaziados >= settings.RAID_HISTORICOS_ESVAZIADOS_MIN
    sinais = sum((duplicados >= settings.RAID_CANAIS_DUPLICADOS_MIN,
                 len(extras) >= settings.RAID_OFFLINE_CANAIS_EXTRAS_SINAL,
                 cargos_faltando >= settings.RAID_OFFLINE_CARGOS_AUSENTES_SINAL,
                 cargos_alterados >= settings.RAID_OFFLINE_CARGOS_ALTERADOS_SINAL,
                 canais_alterados >= settings.RAID_OFFLINE_CANAIS_ALTERADOS_SINAL, config_alterada,
                 topicos_repetidos, mensagens_repetidas, historico_sumiu))
    log.info(
        "inspeção após reconexão: canais ausentes=%d extras=%d duplicados=%d; "
        "cargos ausentes=%d alterados=%d; canais alterados=%d; tópicos repetidos=%s; "
        "spam repetido=%s; histórico esvaziado=%s",
        faltando, len(extras), duplicados, cargos_faltando, cargos_alterados,
        canais_alterados, topicos_repetidos, mensagens_repetidas, historico_sumiu,
    )
    # Uma alteração legítima ou uma correspondência ambígua sozinha não dispara limpeza.
    if not (devastacao or (grave and sinais >= 1) or sinais >= 2
            or duplicados >= settings.RAID_CANAIS_DUPLICADOS_MIN
            or topicos_repetidos
            or mensagens_repetidas or historico_sumiu):
        return False
    log.warning("possível raid offline: canais ausentes=%d extras=%d duplicados=%d; cargos ausentes=%d alterados=%d; canais alterados=%d; sinais=%d",
                faltando, len(extras), duplicados, cargos_faltando, cargos_alterados, canais_alterados, sinais)
    # A auditoria ajuda atribuição, mas não prova culpa de quem só adicionou um bot.
    suspeitos = Counter()
    ids_extras = {c.id for c in extras}
    try:
        limite = discord.utils.utcnow() - timedelta(hours=3)
        async for entrada in guild.audit_logs(limit=100):
            if entrada.created_at < limite:
                break
            try:
                alvo = getattr(entrada.target, "id", None)
            except (TypeError, ValueError, AttributeError, KeyError):
                log.debug("entrada de auditoria sem alvo utilizável ignorada")
                continue
            correlacionada = (
                (entrada.action == discord.AuditLogAction.channel_create and alvo in ids_extras)
                or (entrada.action == discord.AuditLogAction.channel_delete and grave)
                or (entrada.action == discord.AuditLogAction.role_delete and cargos_faltando >= settings.RAID_OFFLINE_CARGOS_AUSENTES_SINAL)
            )
            if correlacionada and entrada.user and not raid.protegido(entrada.user.id):
                suspeitos[entrada.user.id] += 1
    except discord.HTTPException as exc:
        log.warning("auditoria offline indisponível: %s", exc)
    await incidente.iniciar(guild, "Dano estrutural detectado após reconexão")
    for uid, quantidade in suspeitos.items():
        if quantidade >= settings.RAID_OFFLINE_AUDITORIA_AUTOR_MIN:
            await incidente.sancionar(guild, uid, "Atividade destrutiva correlacionada na auditoria", banir=True)
    for uid, webhook_id, tipo in autores_spam:
        if webhook_id:
            await raid._punir_webhook(guild, int(webhook_id), "Spam repetido durante desconexão")
        elif tipo == "aplicativo" and uid and not raid.protegido(int(uid)):
            await raid.aplicar_sancao(guild, int(uid), "medio", "Spam por aplicativo detectado após reconexão", categoria="aplicativo_externo")
        elif uid and not raid.protegido(int(uid)):
            await incidente.sancionar(guild, int(uid), "Spam repetido em vários canais durante desconexão", banir=(tipo == "bot"))
    return True
