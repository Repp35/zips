"""Detecção por autor, auditoria correlacionada e sanções coordenadas por incidente."""

import asyncio
import json
import logging
import re
import time
from collections import defaultdict, deque
from datetime import timedelta

import discord

from ..core.cache import TTLCache
from ..config.settings import CORES
from ..config import settings
from ..data import store
from ..ui.emojis import E
from . import context, links, notify, ocr, palavroes, papeis, regras, rest, incidente

log = logging.getLogger("raid")

# Banir via Discord remove mensagens do autor apenas dentro deste intervalo;
# a API limita o campo a sete dias.
BAN_LIMPAR_MENSAGENS_S = 7 * 24 * 60 * 60

_janelas: dict[tuple[str, int], deque[float]] = defaultdict(deque)
_reincidencia: dict[int, deque[float]] = defaultdict(deque)
_ultima_punicao: dict[int, float] = {}
_em_punicao: set[int] = set()
_vistos: deque[int] = deque(maxlen=1000)
_chamadas = 0
_JANELA_MAXIMA_S = 3600
_ATENCAO_COMANDO_S = settings.RAID_ATENCAO_COMANDO_S
_ATENCAO_BOT_S = settings.RAID_ATENCAO_APLICATIVO_S
_atencao_comandos: dict[int, tuple[float, int, str, int, int]] = {}
_atencao_bots: dict[tuple[int, int], tuple[float, int, str]] = {}
_autores_auditoria: dict[tuple[int, int, int], tuple[float, discord.abc.User]] = {}
_esperando_auditoria: dict[tuple[int, int, int], asyncio.Event] = {}
_COMANDOS_DESTRUTIVOS = {
    "nuke", "raid", "spam", "destroy", "serverwipe", "wipe", "massban",
    "banall", "masskick", "kickall", "purgeall", "clearall", "deleteall",
    "massdelete", "channelwipe", "rolewipe", "threadspam", "webhookspam",
}
_SINAIS_DESTRUTIVOS = re.compile(
    r"\b(?:nuk(?:e|ing)|raid|destroy|mass\s*ban|ban\s*all|mass\s*kick|kick\s*all|"
    r"purge\s*all|clear\s*all|delete\s*all|mass\s*delete|spam|"
    r"wipe|server\s*wipe|destroy\s*(?:the\s*)?server|"
    r"channel\s*wipe|role\s*wipe|thread\s*spam|webhook\s*spam)\b",
    re.IGNORECASE,
)
# Tipos cobertos pelo anti-spam (mensagens); todo o resto é anti-raid.
TIPOS_SPAM = frozenset({"mensagem", "bot_msg", "webhook_msg", "mencao_msg"})
# Config vinda do banco (painel do anti-raid). O painel chama invalidar() ao gravar.
_cfg = TTLCache(5)


def _varrer(agora: float) -> None:
    for chave in [
        k for k, f in _janelas.items() if not f or agora - f[-1] > _JANELA_MAXIMA_S
    ]:
        del _janelas[chave]
    for uid in [
        u
        for u, f in _reincidencia.items()
        if not f or agora - f[-1] > settings.RAID_REINCIDENCIA_S
    ]:
        del _reincidencia[uid]
    for uid in [
        u
        for u, t in _ultima_punicao.items()
        if agora - t > settings.RAID_REINCIDENCIA_S
    ]:
        del _ultima_punicao[uid]


def flag(chave: str) -> bool:
    """Liga/desliga: o banco tem a palavra final; sem valor no banco, vale o padrão de settings (config.json,
    seção editaveis); sem padrão nenhum, fica ligado."""
    valor = _cfg.get(chave)
    if valor is None:
        valor = store.ajuste(chave) or "1"
        _cfg.set(chave, valor)
    return valor == "1"


def ligado() -> bool:
    """Anti-raid: contagem por autor (canais, threads, webhooks, cargos, convites, bans, expulsões, bots, menções)."""
    return flag("antiraid_ativo")


def spam_ligado() -> bool:
    return flag("spam_ativo")


def _comando_destrutivo(texto: str) -> str | None:
    """Reconhece um comando no início da mensagem, com o prefixo configurado ou comum."""
    texto = (texto or "").strip()
    prefixos = {str(settings.PREFIX or ""), ".", ">", "!", "/", "?", "$"} - {""}
    for prefixo in sorted(prefixos, key=len, reverse=True):
        if not texto.startswith(prefixo):
            continue
        resto = texto[len(prefixo):].lstrip()
        token = re.match(r"([\w-]+)", resto, re.UNICODE)
        if token is None:
            continue
        nome = re.sub(r"[^a-z0-9]", "", token.group(1).casefold())
        restante = resto[token.end():].lstrip().casefold()
        primeiro = restante.split(maxsplit=1)[0] if restante else ""
        if nome in {"ban", "kick", "purge", "clear", "delete"} and primeiro == "all":
            nome += "all"
        if nome in _COMANDOS_DESTRUTIVOS:
            return nome
    return None


def observar_comando_suspeito(message: discord.Message) -> None:
    """Marca contexto de atenção; texto isolado nunca causa punição ou contenção."""
    guild = message.guild
    if guild is None or protegido(message.author.id):
        return
    agora = time.monotonic()
    origem = _atencao_comandos.get(guild.id)
    if origem and origem[0] <= agora:
        _atencao_comandos.pop(guild.id, None)
        origem = None

    if not message.author.bot:
        comando = _comando_destrutivo(message.content)
        if comando:
            _atencao_comandos[guild.id] = (
                agora + _ATENCAO_COMANDO_S,
                message.author.id,
                comando,
                message.channel.id,
                message.id,
            )
        return

    # Resposta de app: aproveita o usuário que o Discord fornece quando há
    # interaction_metadata, e só observa o bot se a própria resposta for suspeita.
    if not _SINAIS_DESTRUTIVOS.search(message.content or ""):
        return
    invocador = _invocador(message)
    if invocador is not None:
        if origem and origem[1] != invocador.id:
            return
        uid = invocador.id
        comando = origem[2] if origem else "interação de aplicativo"
    elif (
        origem
        and message.channel.id == origem[3]
        and getattr(getattr(message, "reference", None), "message_id", None) == origem[4]
    ):
        # Prefix commands de terceiros nem sempre têm interaction_metadata.
        # Sem esse metadado, só associa uma resposta que referencia exatamente
        # a mensagem do comando, nunca um bot aleatório no servidor.
        uid, comando = origem[1], origem[2]
    else:
        return
    _atencao_bots[(guild.id, message.author.id)] = (
        agora + _ATENCAO_BOT_S, uid, comando
    )
    _atencao_comandos.pop(guild.id, None)
    if len(_atencao_bots) > 256:
        for chave, valor in list(_atencao_bots.items()):
            if valor[0] <= agora:
                _atencao_bots.pop(chave, None)


def contexto_atencao(guild_id: int, ator_id: int):
    """Contexto de urgência recente; a detecção comportamental não depende dele."""
    agora = time.monotonic()
    chave = (guild_id, ator_id)
    bot = _atencao_bots.get(chave)
    if bot:
        if bot[0] > agora:
            return {"usuario": bot[1], "comando": bot[2], "confirmado": True}
        _atencao_bots.pop(chave, None)
    origem = _atencao_comandos.get(guild_id)
    if origem:
        if origem[0] > agora:
            return {"usuario": origem[1], "comando": origem[2], "confirmado": False}
        _atencao_comandos.pop(guild_id, None)
    return None


async def conter_acao_em_atencao(guild: discord.Guild, ator, acao: str) -> bool:
    """Acelera contenção para ação destrutiva corroborada, seja de membro ou bot."""
    if ator is None or protegido(ator.id):
        return False
    contexto = contexto_atencao(guild.id, ator.id)
    if contexto is None:
        return False
    # Membro: correlaciona só a ação do autor do comando. Bot: exige resposta
    # suspeita atribuída ao próprio bot; nunca associa qualquer bot ao comando
    # mais recente de outra pessoa no servidor.
    if getattr(ator, "bot", False):
        if not contexto["confirmado"]:
            return False
    elif contexto["confirmado"] or contexto["usuario"] != ator.id:
        return False
    motivo = f"Ação destrutiva após comando {contexto['comando']}: {acao}"
    await punir(guild, ator.id, motivo)
    if getattr(ator, "bot", False) and contexto["usuario"] != ator.id and not protegido(contexto["usuario"]):
        await punir(
            guild,
            contexto["usuario"],
            f"Acionou comando {contexto['comando']} que iniciou ação destrutiva",
        )
    return True


def limites_personalizados() -> dict[str, tuple[int, int]]:
    """Limites que a equipe mudou pelo painel. Tipos que não aparecem aqui usam settings.RAID_LIMITES."""
    cached = _cfg.get("raid_limites")
    if cached is None:
        try:
            bruto = json.loads(store.get_config("raid_limites") or "{}")
            cached = {
                tipo: (int(par[0]), int(par[1]))
                for tipo, par in bruto.items()
                if isinstance(par, (list, tuple))
                and len(par) == 2
                and 1 <= int(par[0]) <= 1000
                and 1 <= int(par[1]) <= 3600
            }
        except (ValueError, TypeError, KeyError, IndexError, AttributeError):
            cached = {}
        _cfg.set("raid_limites", cached)
    return cached


def limites() -> dict[str, tuple[int, int]]:
    return {**settings.RAID_LIMITES, **limites_personalizados()}


def invalidar() -> None:
    """Chamado pelo painel depois de gravar, para a próxima checagem já usar o valor novo."""
    _cfg.clear()


def estourou(tipo: str, ator_id: int) -> bool:
    """Registra uma ação do autor e diz se ele passou do máximo permitido na janela do tipo.
    Com a proteção do tipo desligada, não conta nada (anti-spam para mensagens, anti-raid para o resto)."""
    if not (spam_ligado() if tipo in TIPOS_SPAM else ligado()):
        return False
    config_tipo = "mencao" if tipo == "mencao_msg" else tipo
    maximo, janela = limites_personalizados().get(
        config_tipo
    ) or settings.RAID_LIMITES.get(
        config_tipo, (settings.SPAM_MAX_MSGS, settings.SPAM_JANELA_S)
    )
    global _chamadas
    agora = time.monotonic()
    _chamadas += 1
    if _chamadas >= 500:
        _chamadas = 0
        _varrer(agora)
    fila = _janelas[(tipo, ator_id)]
    while fila and agora - fila[0] > janela:
        fila.popleft()
    fila.append(agora)
    while len(fila) > maximo + 1:
        fila.popleft()
    if len(fila) == 1 and tipo not in TIPOS_SPAM:
        log.info(
            "ação estrutural sob observação: tipo=%s autor=%s limite=%d janela=%ds",
            tipo,
            ator_id,
            maximo,
            janela,
        )
    excedeu_limite = len(fila) > maximo
    if excedeu_limite and len(fila) == maximo + 1:
        log.warning(
            "limite anti-raid excedido: tipo=%s autor=%s ações=%d janela=%ds",
            tipo,
            ator_id,
            len(fila),
            janela,
        )
    return excedeu_limite


def _bot_id() -> int | None:
    try:
        user = context.bot().user
    except RuntimeError:
        return None
    return user.id if user is not None else None


def protegido(ator_id: int) -> bool:
    """Só o dono real e este bot são imunes; cargos de equipe não dão imunidade."""
    if ator_id == _bot_id():
        return True
    try:
        guild = context.bot().get_guild(settings.GUILD_ID)
        return ator_id == (guild.owner_id if guild else settings.ADMIN_ID)
    except RuntimeError:
        return ator_id == settings.ADMIN_ID


def excedeu(tipo: str, ator: discord.abc.User | int | None) -> bool:
    if ator is None:
        return False
    ator_id = getattr(ator, "id", ator)
    return not protegido(ator_id) and estourou(tipo, ator_id)


async def punir(
    guild: discord.Guild,
    ator_id: int | None,
    motivo: str,
    detalhes=(),
    tirar_cargos: bool = False,
) -> None:
    """Toda detecção abre contenção; sanções ficam fora da reversão do backup."""
    if incidente.ativo(guild.id):
        incidente.atividade(guild)
    if ator_id is None or ator_id in _em_punicao or protegido(ator_id):
        return
    _em_punicao.add(ator_id)
    try:
        await incidente.iniciar(guild, motivo)
        anterior = incidente.estado(guild.id).get("alvos", {}).get(str(ator_id))
        destrutivo = any(
            p in motivo.lower()
            for p in (
                "exclus",
                "bans",
                "expuls",
                "webhook",
                "edição",
                "cargos",
                "canais",
                "destrutiva",
            )
        )
        ok = await incidente.sancionar(
            guild, ator_id, motivo, banir=destrutivo or bool(anterior)
        )
        await incidente.vinculos(guild, ator_id)
        await _registrar(ator_id, motivo, "contenção", ok, detalhes)
    finally:
        _em_punicao.discard(ator_id)


async def _mutar(guild: discord.Guild, ator_id: int, motivo: str) -> bool:
    try:
        membro = guild.get_member(ator_id) or await guild.fetch_member(ator_id)
        await membro.timeout(timedelta(seconds=settings.SPAM_TIMEOUT_S), reason=motivo)
        return True
    except discord.HTTPException as exc:
        log.warning("mute falhou em %s: %s", ator_id, exc)
        return False


async def _tirar_cargos(guild: discord.Guild, ator_id: int, motivo: str) -> list[str]:
    """Tira de volta todos os cargos que o bot consegue remover. Devolve os nomes removidos."""
    try:
        membro = guild.get_member(ator_id) or await guild.fetch_member(ator_id)
    except discord.NotFound:
        return []
    except discord.HTTPException as exc:
        log.warning("não consegui consultar cargos de %s: %s", ator_id, exc)
        return []
    cargo_membro = papeis.fixo(guild, "membro")
    cargos = [
        r for r in membro.roles
        if r != guild.default_role and r != cargo_membro and r.is_assignable()
    ]
    if not cargos:
        return []
    try:
        await membro.remove_roles(*cargos, reason=motivo)
    except discord.HTTPException as exc:
        log.warning("não consegui tirar cargos de %s: %s", ator_id, exc)
        return []
    # Guarda os IDs, para o /untimeout poder devolver se o dono pedir.
    guardados = set(json.loads(store.get_config(f"raid:cargos:{ator_id}") or "[]"))
    guardados |= {r.id for r in cargos}
    store.set_config(f"raid:cargos:{ator_id}", json.dumps(sorted(guardados)))
    return [r.name for r in cargos]


async def _banir(guild: discord.Guild, ator_id: int, motivo: str) -> bool:
    try:
        await guild.ban(
            discord.Object(id=ator_id), reason=motivo,
            delete_message_seconds=BAN_LIMPAR_MENSAGENS_S,
        )
        await avisar_ban(guild, ator_id, motivo)
        return True
    except discord.HTTPException as exc:
        log.warning("ban falhou em %s: %s", ator_id, exc)
        return False


async def avisar_ban(guild: discord.Guild, uid: int, motivo: str) -> None:
    """Envia explicação em Components V2 após ban por infração; DM fechada não afeta a sanção."""
    from ..ui import layout

    try:
        user = context.bot().get_user(uid) or await context.bot().fetch_user(uid)
        motivo_seguro = discord.utils.escape_markdown(str(motivo))[:700]
        payload = layout.mensagem(
            layout.cartao(
                layout.titulo("Banimento aplicado"),
                layout.texto(
                    f"Você foi banido permanentemente do servidor **{discord.utils.escape_markdown(guild.name)}**.\n\n"
                    f"**Motivo registrado:** {motivo_seguro}\n\n"
                    "Se você acredita que isso foi um engano, entre em contato com a equipe do servidor para solicitar uma revisão."
                ),
                cor=CORES["erro"],
            )
        )
        await rest.send_dm(user, payload)
    except discord.HTTPException as exc:
        log.info("aviso de ban por DM não entregue a %s: %s", uid, exc)
    except RuntimeError:
        log.warning("bot indisponível para enviar aviso de ban a %s", uid)


async def _registrar(
    ator_id: int, motivo: str, acao: str, ok: bool, detalhes=()
) -> None:
    await log_acesso(
        f"{E.alert} Anti-raid: {acao}",
        CORES["erro"],
        [
            ("Autor", f"<@{ator_id}> ({ator_id})"),
            ("Motivo", motivo),
            *detalhes,
            ("Ação", acao),
            ("Resultado", "aplicada" if ok else "falhou"),
        ],
    )


async def log_acesso(titulo: str, cor: int, campos) -> None:
    """Registro de auditoria do anti-raid, enviado ao canal de logs configurado."""
    await notify.registrar("raid_punicao", titulo, cor, campos)


# ── Log de auditoria ──────────────────────────────────────────────────────────


async def autor_recente(
    guild: discord.Guild, acao: discord.AuditLogAction, alvo_id: int | None = None
):
    """Usa primeiro o evento de auditoria em cache; faz no máximo uma consulta curta como fallback."""
    chave = (guild.id, int(acao.value), int(alvo_id or 0))
    cacheado = _autores_auditoria.get(chave)
    if cacheado and time.monotonic() - cacheado[0] <= settings.RAID_AUDIT_JANELA_S:
        return cacheado[1]
    evento = _esperando_auditoria.setdefault(chave, asyncio.Event())
    try:
        await asyncio.wait_for(evento.wait(), timeout=0.2)
    except asyncio.TimeoutError:
        pass
    cacheado = _autores_auditoria.get(chave)
    if cacheado and time.monotonic() - cacheado[0] <= settings.RAID_AUDIT_JANELA_S:
        _esperando_auditoria.pop(chave, None)
        return cacheado[1]
    _esperando_auditoria.pop(chave, None)
    agora = discord.utils.utcnow()
    try:
        async for entrada in guild.audit_logs(limit=10, action=acao):
            if (agora - entrada.created_at).total_seconds() > settings.RAID_AUDIT_JANELA_S:
                break
            if alvo_id is not None:
                try:
                    alvo_entry_id = getattr(entrada.target, "id", None)
                except (TypeError, ValueError, AttributeError, KeyError):
                    continue
                if alvo_entry_id != alvo_id:
                    continue
            if entrada.user is not None:
                _guardar_autor_auditoria(entrada)
            return entrada.user
    except discord.HTTPException as exc:
        log.warning("auditoria indisponível (%s): %s", acao, exc)
    return None


def _guardar_autor_auditoria(entrada) -> None:
    # `AuditLogEntry.target` é lazy e discord.py pode tentar construir um
    # discord.Object(id=None) em eventos parciais do gateway. Não deixe esse
    # payload incompleto derrubar o listener inteiro.
    try:
        alvo = entrada.target
    except (TypeError, ValueError, AttributeError, KeyError):
        alvo = None
    alvo_id = getattr(alvo, "id", None)
    autor = getattr(entrada, "user", None)
    guild_id = getattr(getattr(entrada, "guild", None), "id", None)
    acao = getattr(entrada, "action", None)
    if alvo_id is None or autor is None or guild_id is None or acao is None:
        return
    agora = time.monotonic()
    chave = (guild_id, int(acao.value), int(alvo_id))
    _autores_auditoria[chave] = (agora, autor)
    aguardando = _esperando_auditoria.get(chave)
    if aguardando is not None:
        aguardando.set()
    if len(_autores_auditoria) > 2048:
        for antiga, (instante, _) in list(_autores_auditoria.items()):
            if agora - instante > settings.RAID_AUDIT_JANELA_S:
                _autores_auditoria.pop(antiga, None)


async def _entradas_novas(guild: discord.Guild, acao: discord.AuditLogAction) -> list:
    """Entradas recentes ainda não processadas (evita contar a mesma ação duas vezes)."""
    agora = discord.utils.utcnow()
    novas = []
    try:
        async for entrada in guild.audit_logs(limit=50, action=acao):
            if (
                agora - entrada.created_at
            ).total_seconds() > settings.RAID_AUDIT_JANELA_S:
                break
            if entrada.id in _vistos:
                continue
            _vistos.append(entrada.id)
            novas.append(entrada)
    except discord.HTTPException as exc:
        log.warning("auditoria indisponível (%s): %s", acao, exc)
    return novas


async def _apagar(alvo) -> None:
    try:
        await alvo.delete()
    except discord.NotFound:
        return
    except discord.HTTPException as exc:
        log.error("anti-raid não conseguiu remover recurso %s: %s", getattr(alvo, "id", "?"), exc)


# ── Canais e threads ──────────────────────────────────────────────────────────


async def thread_criada(thread: discord.Thread) -> None:
    if not ligado():
        return
    agora = time.monotonic()
    dono = thread.owner_id
    repetidas = sum(
        t.parent_id == thread.parent_id and t.name == thread.name
        for t in thread.guild.threads
    ) >= settings.RAID_TOPICOS_DUPLICADOS_MIN
    if dono is None:
        fila = _threads_sem_criador[thread.parent_id or thread.id]
        _janela(fila, agora)
        fila.append((agora, thread.id))
        if len(fila) >= _limite_threads() or repetidas:
            await incidente.iniciar(thread.guild, "Criação massiva de tópicos sem autor confirmado")
            await _apagar(thread)
            fila.clear()
            await log_acesso(
                f"{E.alert} Criação rápida de threads sem autor identificado",
                CORES["erro"],
                [("Canal", getattr(thread.parent, "mention", "?"))],
            )
        return
    if protegido(dono):
        return
    fila = _threads_por_criador[dono]
    _janela(fila, agora)
    fila.append((agora, thread.id))
    if len(fila) >= _limite_threads() or repetidas:
        await incidente.iniciar(thread.guild, "Criação massiva de tópicos")
        await _apagar(thread)
        fila.clear()
        await aplicar_sancao(thread.guild, dono, "medio", "Criação rápida de tópicos", [("Canal", getattr(thread.parent, "mention", "?"))], categoria="criacao_topicos")


def _janela(fila: deque, agora: float) -> None:
    while fila and agora - fila[0][0] > _janela_threads():
        fila.popleft()


def _janela_threads() -> int:
    return limites().get("thread_create", (2, 5))[1]


def _limite_threads() -> int:
    # O valor do painel é a quantidade permitida; a próxima ação confirma o excesso.
    return limites().get("thread_create", (2, 5))[0] + 1


async def canal_editado(channel: discord.abc.GuildChannel) -> None:
    if not ligado():
        return
    autor = await autor_recente(
        channel.guild, discord.AuditLogAction.channel_update, channel.id
    )
    if excedeu("channel_update", autor):
        from . import backup

        backup.agendar_restauracao(channel.guild)
        await punir(channel.guild, autor.id, "Edição de canais em massa")


# ── Webhooks ──────────────────────────────────────────────────────────────────


async def apagar_webhook(webhook_id: int) -> None:
    try:
        webhook = await context.bot().fetch_webhook(webhook_id)
        await webhook.delete(reason="Anti-raid")
    except discord.NotFound:
        pass
    except discord.HTTPException as exc:
        log.warning("não foi possível apagar webhook %s: %s", webhook_id, exc)
    store.forget_webhook(str(webhook_id))


async def _punir_webhook(
    guild: discord.Guild, webhook_id: int, motivo: str, canal=None
) -> None:
    criador = store.webhook_creator(str(webhook_id))
    if criador is None:
        try:
            webhook = await context.bot().fetch_webhook(webhook_id)
            criador = getattr(webhook.user, "id", None)
            if criador:
                store.save_webhook_creator(str(webhook_id), criador)
        except discord.NotFound:
            pass
        except discord.HTTPException as exc:
            log.warning("não consegui consultar criador do webhook %s: %s", webhook_id, exc)
    await apagar_webhook(webhook_id)
    detalhes = [
        ("Webhook", webhook_id),
        ("Criador", f"<@{criador}> ({criador})" if criador else "desconhecido"),
    ]
    if canal is not None:
        detalhes.append(("Local", getattr(canal, "mention", str(canal))))
    # A autoria de criação do webhook não demonstra quem enviou cada mensagem.
    await incidente.iniciar(guild, motivo)
    if criador and not protegido(int(criador)):
        await aplicar_sancao(
            guild,
            int(criador),
            "grave",
            "Webhook usado para spam; criador retido para análise",
            detalhes,
            categoria="spam_webhook",
        )
    await log_acesso(
        f"{E.alert} Webhook neutralizado", CORES["erro"], [("Motivo", motivo), *detalhes]
    )


async def webhooks_criados(guild: discord.Guild) -> None:
    if not ligado():
        return
    for entrada in await _entradas_novas(guild, discord.AuditLogAction.webhook_create):
        alvo, ator = entrada.target, entrada.user
        if alvo is None or ator is None:
            continue
        store.save_webhook_creator(str(alvo.id), ator.id)
        campos = [("Webhook", alvo.id), ("Criador", f"<@{ator.id}> ({ator.id})")]
        if excedeu("webhook_create", ator):
            await apagar_webhook(alvo.id)
            await punir(guild, ator.id, "Criação de webhooks em massa", campos)


async def mensagem_webhook(message: discord.Message) -> bool:
    wid = message.webhook_id
    if await _spam_cruzado(message):
        return True
    if not (
        estourou("webhook_msg", wid)
        or (_mencoes_em_massa(message) and estourou("mencao_msg", wid))
    ):
        return False
    await _apagar(message)
    await _punir_webhook(
        message.guild, wid, "Spam ou menções em massa por webhook", message.channel
    )
    return True


# ── Bots ──────────────────────────────────────────────────────────────────────


def _invocador(message: discord.Message):
    """Quem usou o comando de um bot externo (slash command), quando o Discord informa."""
    metadata = getattr(message, "interaction_metadata", None)
    usuario = getattr(metadata, "user", None)
    if usuario is None and isinstance(metadata, dict):
        usuario = metadata.get("user")
    if isinstance(usuario, dict):
        return discord.Object(id=int(usuario["id"])) if usuario.get("id") else None
    return usuario


def mensagem_aplicativo_externo(message: discord.Message) -> bool:
    """Mensagens públicas de aplicativos externos carregam o usuário da interação."""
    if _invocador(message) is None or not getattr(message.author, "bot", False):
        return False
    metadata = getattr(message, "interaction_metadata", None)
    # Discord.py expõe exatamente a origem user-installed pelo método abaixo.
    # Em payloads antigos, a presença da interação ainda é evidência suficiente.
    classifica = getattr(metadata, "is_user_integration", None)
    return bool(classifica()) if callable(classifica) else True


async def bot_entrou(member: discord.Member) -> None:
    if not ligado() or not member.bot:
        return
    autor = await autor_recente(member.guild, discord.AuditLogAction.bot_add, member.id)
    if autor is None:
        return
    store.save_bot_adder(member.id, autor.id)
    campos = [
        ("Bot", f"<@{member.id}> ({member.id})"),
        ("Adicionado por", f"<@{autor.id}> ({autor.id})"),
    ]
    if excedeu("bot_add", autor):
        try:
            motivo = "Anti-raid: bot adicionado durante uma sequência de convites suspeita"
            await member.guild.ban(
                member, reason=motivo,
                delete_message_seconds=BAN_LIMPAR_MENSAGENS_S,
            )
            await avisar_ban(member.guild, member.id, motivo)
        except discord.HTTPException as exc:
            log.error("não consegui banir permanentemente o bot %s: %s", member.id, exc)
        await punir(member.guild, autor.id, "Adição de bots em massa", campos)


async def pedir_confirmacao_bot(member: discord.Member) -> None:
    """Pergunta ao dono, no canal de logs, se o bot novo é seguro e qual cargo ganha (ou nenhum). Só o dono decide."""
    autor = await autor_recente(member.guild, discord.AuditLogAction.bot_add, member.id)
    quem = f"<@{autor.id}> ({autor.id})" if autor else "não identificado"
    canal = int(store.ajuste("logs_channel_id") or settings.LOGS_CHANNEL_ID or 0)
    if not canal:
        log.warning(
            "bot %s entrou, mas não há canal de logs para pedir confirmação", member.id
        )
        return
    payload = {
        "content": f"<@{settings.ADMIN_ID}> {E.alert} **Bot novo no servidor:** <@{member.id}> ({member.id})\n"
        f"Adicionado por: {quem}\nÉ seguro? Escolha o cargo que ele recebe, se algum:",
        "components": [
            {
                "type": 1,
                "components": [
                    {
                        "type": 2,
                        "style": 3,
                        "label": f"Dar cargo {settings.CARGO_BOTS}",
                        "custom_id": f"bot:ok:{member.id}",
                    },
                    {
                        "type": 2,
                        "style": 2,
                        "label": "Sem cargo",
                        "custom_id": f"bot:sem:{member.id}",
                    },
                    {
                        "type": 2,
                        "style": 4,
                        "label": "Recusar e expulsar",
                        "custom_id": f"bot:no:{member.id}",
                    },
                ],
            }
        ],
    }
    try:
        await rest.send_message(canal, payload)
    except RuntimeError as exc:
        log.error("não consegui pedir confirmação do bot %s: %s", member.id, exc)


# ── Spam sequencial, timeout e reincidência (regra única para membros e bots) ─────────────

_sequencias = regras.DetectorSequencia()
_threads_por_criador: dict[int, deque[tuple[float, int]]] = defaultdict(deque)
_threads_sem_criador: dict[int, deque[tuple[float, int]]] = defaultdict(deque)


def _timeouts() -> dict[str, dict]:
    """uid -> {"ate": ISO, "renovar": bool}. Só os timeouts que o próprio bot aplicou."""
    bruto = json.loads(store.get_config("raid:timeouts") or "{}")
    return {
        uid: (v if isinstance(v, dict) else {"ate": v, "renovar": True})
        for uid, v in bruto.items()
    }


def _salvar_timeouts(d: dict[str, dict]) -> None:
    store.set_config("raid:timeouts", json.dumps(d))


def _ocorrencias() -> dict[str, list[float]]:
    return json.loads(store.get_config("raid:ocorrencias") or "{}")


def _registrar_ocorrencia(uid: int, categoria: str) -> int:
    """Conta reincidência por método, sem misturar tipos diferentes de abuso."""
    agora = time.time()
    d = _ocorrencias()
    chave = f"{uid}:{categoria}"
    lista = regras.ocorrencias_na_janela(d.get(chave, []), agora) + [agora]
    d[chave] = lista
    store.set_config("raid:ocorrencias", json.dumps(d))
    return len(lista)


async def _aplicar_timeout(guild: discord.Guild, uid: int, motivo: str, segundos: int, renovar: bool) -> bool:
    """Timeout pelo tempo do passo. Fica registrado como aplicado pelo bot, para a renovação."""
    segundos = max(1, min(int(segundos), regras.TIMEOUT_SPAM_S))
    try:
        membro = guild.get_member(uid) or await guild.fetch_member(uid)
        await membro.timeout(timedelta(seconds=segundos), reason=motivo)
    except discord.NotFound:
        _timeouts().pop(str(uid), None)
        return False
    except discord.HTTPException as exc:
        log.warning("timeout falhou em %s: %s", uid, exc)
        return False
    d = _timeouts()
    d[str(uid)] = {
        "ate": (discord.utils.utcnow() + timedelta(seconds=segundos)).isoformat(),
        "renovar": renovar,
        "duracao_s": min(int(segundos), regras.TIMEOUT_SPAM_S),
    }
    _salvar_timeouts(d)
    return True


async def renovar_timeouts(guild: discord.Guild) -> None:
    """Passo com renovar=True (quase infinito): renova todo dia enquanto o timeout está ativo, até virar ban.
    Só renova o que o bot aplicou. Se o campo communication_disabled_until veio nulo, um moderador removeu:
    o bot não reaplica. Timeouts sem renovação só são limpos depois de vencer."""
    d = _timeouts()
    mudou = False
    agora = discord.utils.utcnow()
    for uid_s in list(d):
        info = d[uid_s]
        try:
            membro = guild.get_member(int(uid_s)) or await guild.fetch_member(int(uid_s))
        except discord.NotFound:
            d.pop(uid_s, None)
            mudou = True
            continue
        except discord.HTTPException as exc:
            # Uma falha temporária de API não apaga o estado necessário para
            # renovar o castigo na próxima passagem.
            log.warning("não consegui verificar timeout de %s; estado preservado: %s", uid_s, exc)
            continue
        ate_atual = membro.communication_disabled_until
        if ate_atual is None:
            d.pop(uid_s, None)
            mudou = True
            await log_acesso(
                f"{E.alert} Timeout removido por moderador: não reaplicado",
                CORES["info"],
                [("Membro", f"<@{uid_s}> ({uid_s})")],
            )
            continue
        if not info.get("renovar"):
            continue
        duracao = min(
            max(60, int(info.get("duracao_s", regras.TIMEOUT_SPAM_S))),
            regras.TIMEOUT_SPAM_S,
        )
        if ate_atual - agora < timedelta(seconds=min(duracao / 3, 24 * 3600)):
            try:
                await membro.timeout(timedelta(seconds=duracao), reason="Renovação do timeout de spam")
                info["ate"] = (agora + timedelta(seconds=duracao)).isoformat()
                mudou = True
            except discord.HTTPException as exc:
                log.warning("não renovei timeout de %s: %s", uid_s, exc)
    if mudou:
        _salvar_timeouts(d)


async def remover_timeout(
    guild: discord.Guild, membro: discord.Member, quem: str, devolver_cargos: bool = False
) -> str:
    """Tira o timeout agora e apaga o registro do membro, para o bot não renovar.
    Por padrão não devolve cargos. Com devolver_cargos=True, devolve os que o bot tirou na punição.
    Não desbane. Não cancela uma punição futura por nova infração."""
    d = _timeouts()
    tinha_registro = d.pop(str(membro.id), None) is not None
    _salvar_timeouts(d)  # antes da API: se a API falhar, o bot ainda não renova
    try:
        await membro.timeout(None, reason=f"Timeout removido pelo dono ({quem})")
    except discord.HTTPException as exc:
        return f"{E.wrong} Não consegui remover o timeout de {membro.mention}: {exc}"

    devolvidos: list[str] = []
    aviso_cargos = ""
    if devolver_cargos:
        ids = json.loads(store.get_config(f"raid:cargos:{membro.id}") or "[]")
        cargos = [guild.get_role(int(i)) for i in ids]
        cargos = [c for c in cargos if c is not None and c.is_assignable() and c not in membro.roles]
        if cargos:
            try:
                await membro.add_roles(*cargos, reason=f"Cargos devolvidos pelo dono ({quem})")
                devolvidos = [c.name for c in cargos]
            except discord.HTTPException as exc:
                aviso_cargos = f" Cargos não devolvidos: {exc}."
        elif not ids:
            aviso_cargos = " Não havia cargos guardados para devolver."
        if devolvidos or not aviso_cargos:
            store.set_config(f"raid:cargos:{membro.id}", "[]")

    await log_acesso(
        f"{E.correct} Timeout removido pelo dono",
        CORES["info"],
        [
            ("Membro", f"{membro.mention} ({membro.id})"),
            ("Por", quem),
            ("Renovação", "cancelada" if tinha_registro else "não havia registro do bot"),
            ("Cargos devolvidos", ", ".join(devolvidos) or ("sim" if devolver_cargos else "não (padrão)")),
        ],
    )
    texto_cargos = (
        f" Cargos devolvidos: {', '.join(devolvidos)}." if devolvidos
        else (aviso_cargos or " Cargos não foram devolvidos.")
    )
    return f"{E.correct} Timeout de {membro.mention} removido. O bot não vai renovar.{texto_cargos}"


async def aplicar_sancao(guild: discord.Guild, uid: int, nivel: str, motivo: str, detalhes=(), *, categoria: str | None = None, bot_executor: bool = False) -> None:
    """Aplica a escada do nível: tira cargos (se o passo manda), dá timeout (se houver) ou bane.
    Bot não recebe timeout: para bot, o passo só tira cargos; o ban fica para a reincidência."""
    if protegido(uid):
        return
    categoria = categoria or nivel
    n = _registrar_ocorrencia(uid, categoria)
    passo = regras.sancao(nivel, n)
    membro = guild.get_member(uid)
    if membro is None:
        try:
            membro = await guild.fetch_member(uid)
        except discord.HTTPException:
            membro = None
    eh_bot = bool(membro and membro.bot)
    if passo["ban"] or bot_executor or eh_bot:
        if passo["ban"] and not (bot_executor or eh_bot):
            await _tirar_cargos(guild, uid, motivo)
        ok = await _banir(guild, uid, f"Reincidência ({nivel}, {n}ª vez): {motivo}")
        await _registrar(uid, motivo, f"banimento permanente ({categoria}, {n}ª ocorrência)", ok, detalhes)
        return
    if passo["cargos"]:
        await _tirar_cargos(guild, uid, motivo)
    if passo.get("aviso"):
        try:
            avisado = membro or await guild.fetch_member(uid)
            await avisado.send(
                f"Aviso de moderação em **{guild.name}**: {motivo}. "
                "Se o spam continuar, a próxima ocorrência poderá gerar timeout e a seguinte, banimento permanente."
            )
        except discord.HTTPException:
            log.info("aviso privado não entregue a %s", uid)
        await _registrar(uid, motivo, f"aviso ({categoria}, {n}ª ocorrência)", True, detalhes)
        return
    if passo["timeout_s"] is None:
        await _registrar(
            uid,
            motivo,
            f"cargos removidos ({nivel}, {n}ª ocorrência; bot não recebe timeout)" if eh_bot else f"cargos removidos ({nivel}, {n}ª ocorrência)",
            True,
            detalhes,
        )
        return
    ok = await _aplicar_timeout(guild, uid, motivo, passo["timeout_s"], passo["renovar"])
    duracao = "até virar ban (renovado)" if passo["renovar"] else _duracao(passo["timeout_s"])
    await _registrar(
        uid,
        motivo,
            f"cargos {'removidos' if passo['cargos'] else 'mantidos'}, timeout {duracao} ({categoria}, {n}ª ocorrência)",
        ok,
        detalhes,
    )


def _duracao(segundos: int) -> str:
    if segundos >= regras.DIA:
        return f"de {segundos // regras.DIA} dia(s)"
    return f"de {segundos // regras.HORA or 1} hora(s)" if segundos >= regras.HORA else f"de {max(1, segundos // regras.MINUTO)} minuto(s)"


async def _apagar_ids(canal, ids: list[int]) -> None:
    for mid in ids:
        try:
            await rest.request("DELETE", f"/channels/{canal.id}/messages/{mid}")
        except (rest.DiscordHTTPError, rest.DiscordTransportError):
            pass


async def _bloquear_apps_no_canal(guild: discord.Guild, canal, app_id: int) -> list[str]:
    """Aplicativo sem autor identificável: tira a permissão de Usar Aplicativos Externos no canal,
    para os cargos que possam concedê-la. Escopo de canal, não do servidor."""
    alvo = canal.parent if isinstance(canal, discord.Thread) else canal
    me = guild.get_member(app_id)
    topo = me.top_role if me is not None else None
    alterados: list[str] = []
    if not hasattr(alvo, "set_permissions"):
        return alterados
    for cargo in guild.roles:
        if cargo.managed or (topo is not None and cargo.position >= topo.position):
            continue
        try:
            ow = alvo.overwrites_for(cargo)
            ow.use_external_apps = False
            await alvo.set_permissions(cargo, overwrite=ow, reason="Anti-raid: aplicativo externo em spam")
            alterados.append(cargo.name)
        except (discord.HTTPException, AttributeError) as exc:
            log.warning("não bloqueei apps em %s para %s: %s", alvo.id, cargo.name, exc)
    return alterados


async def _apagar_canais_ids(ids: list[int]) -> None:
    for cid in ids:
        try:
            await rest.request("DELETE", f"/channels/{cid}")
        except (rest.DiscordHTTPError, rest.DiscordTransportError):
            pass


async def _apagar_do_bot_no_canal(canal, bot_id: int, limite: int = 100) -> int:
    alvo = canal.parent if isinstance(canal, discord.Thread) else canal
    try:
        msgs = await rest.request("GET", f"/channels/{canal.id}/messages?limit={limite}")
    except (rest.DiscordHTTPError, rest.DiscordTransportError):
        return 0
    if not isinstance(msgs, list):
        return 0
    ids = [int(m["id"]) for m in msgs if (m.get("author") or {}).get("id") == str(bot_id)]
    await _apagar_ids(canal, ids)
    return len(ids)


async def _spam_sequencial(message: discord.Message) -> bool:
    """5 mensagens seguidas do mesmo autor, com até 3 s entre elas. Apaga a sequência inteira."""
    if not spam_ligado():
        return False
    invocador = _invocador(message) if message.author.bot else None
    autor_contador = invocador.id if invocador is not None else message.author.id
    resultado = _sequencias.registrar(message.channel.id, autor_contador, message.id)
    if resultado is None:
        return False
    apagar, primeira_deteccao = resultado
    await _apagar_ids(message.channel, apagar)
    if not primeira_deteccao:
        return True
    if not message.author.bot:
        atencao = contexto_atencao(message.guild.id, message.author.id)
        if atencao and atencao["usuario"] == message.author.id:
            await punir(message.guild, message.author.id,
                        f"Spam destrutivo após comando {atencao['comando']}")
            return True
        await aplicar_sancao(
            message.guild,
            message.author.id,
            "baixo",
            "Spam de mensagens",
            [("Local", getattr(message.channel, "mention", str(message.channel)))],
            categoria="mensagem_repetida",
        )
        return True
    await _punir_bot_em_spam(message)
    return True


async def _punir_bot_em_spam(message: discord.Message) -> None:
    bot_id = message.author.id
    invocador = _invocador(message)
    local = getattr(message.channel, "mention", str(message.channel))
    atencao_bot = contexto_atencao(message.guild.id, bot_id)
    if atencao_bot and atencao_bot["confirmado"]:
        motivo = f"Ação destrutiva após comando {atencao_bot['comando']}: spam de aplicativo"
        await incidente.iniciar(message.guild, motivo)
        if message.guild.get_member(bot_id) is None:
            # Apps user-installed podem responder sem serem membros do servidor.
            # Tentamos banir o ID; se a API não permitir, bloqueamos Apps externos
            # só no canal atingido e removemos as mensagens do app.
            banido = await _banir(message.guild, bot_id, motivo)
            cargos_bloqueados = []
            apagadas = 0
            if not banido:
                cargos_bloqueados = await _bloquear_apps_no_canal(
                    message.guild, message.channel, bot_id
                )
                apagadas = await _apagar_do_bot_no_canal(message.channel, bot_id)
            await log_acesso(
                f"{E.alert} Aplicativo externo neutralizado",
                CORES["erro"],
                [("Aplicativo", f"<@{bot_id}> ({bot_id})"),
                 ("Ban aplicado", str(banido)),
                 ("Cargos bloqueados no canal", ", ".join(cargos_bloqueados) or "nenhum"),
                 ("Mensagens removidas", str(apagadas)), ("Local", local)],
            )
        else:
            await punir(message.guild, bot_id, motivo, [("Local", local)])
        executor_id = invocador.id if invocador is not None else atencao_bot["usuario"]
        if executor_id != bot_id and not protegido(executor_id):
            await punir(
                message.guild,
                executor_id,
                f"Ação destrutiva após comando {atencao_bot['comando']}: acionou aplicativo em spam",
                [("Aplicativo", f"<@{bot_id}> ({bot_id})"), ("Local", local)],
            )
        return
    if invocador is not None:
        if not protegido(invocador.id):
            await aplicar_sancao(
                message.guild,
                invocador.id,
                "medio",
                "Spam por aplicativo executado pelo usuário",
                [("Local", local), ("Bot", f"<@{bot_id}> ({bot_id})")],
                categoria="aplicativo_externo",
            )
        return
    if getattr(message, "interaction_metadata", None) is not None:
        # Aplicativo externo sem usuário identificável: bloqueia no canal, em vez de punir ninguém.
        alterados = await _bloquear_apps_no_canal(message.guild, message.channel, bot_id)
        apagadas = await _apagar_do_bot_no_canal(message.channel, bot_id)
        await log_acesso(
            f"{E.alert} Aplicativo externo em spam: ação no canal",
            CORES["erro"],
            [
                ("Aplicativo", f"<@{bot_id}> ({bot_id})"),
                ("Local", local),
                ("Cargos sem Usar Aplicativos Externos", ", ".join(alterados) or "nenhum"),
                ("Mensagens apagadas", str(apagadas)),
            ],
        )
        return
    await aplicar_sancao(message.guild, bot_id, "grave", "Spam por bot", [("Local", local)], categoria="spam_bot", bot_executor=True)


async def punir_adicionador(guild: discord.Guild, uid: int) -> None:
    """A contenção do convidador é distinta do ban permanente do bot executor."""
    if protegido(uid):
        return
    st = incidente.estado(guild.id)
    if str(uid) in st.setdefault("adicionadores_contidos", []):
        return
    try:
        membro = guild.get_member(uid) or await guild.fetch_member(uid)
    except discord.NotFound:
        return
    motivo = "Adicionou bot que executou ações maliciosas; revisão da equipe"
    removidos = await _tirar_cargos(guild, uid, motivo)
    contido = await _aplicar_timeout(
        guild, uid, motivo, regras.TIMEOUT_SPAM_S, renovar=True
    )
    if not contido:
        log.warning("contenção de 28 dias do adicionador %s falhou", uid)
        return
    st["adicionadores_contidos"].append(str(uid))
    incidente.salvar(guild.id)
    await _registrar(uid, "Adicionou bot que executou ação maliciosa", "timeout renovável de 28 dias; cargos removíveis retirados", contido, [("Cargos removidos", ", ".join(removidos) or "nenhum")])


# ── Mensagens de bot e humanas ───────────────────────────────────────────────


_repetidas: dict[tuple[int, int, str], deque[tuple[float, int, int]]] = defaultdict(deque)
_repetidas_chamadas = 0


async def _spam_cruzado(message: discord.Message) -> bool:
    """Mesmo texto em canais distintos; contém o autor sem uma rajada de DELETE."""
    global _repetidas_chamadas
    if not spam_ligado() or protegido(message.author.id):
        return False
    texto = " ".join((message.content or "").casefold().split())
    if len(texto) < settings.SPAM_MULTICANAL_TEXTO_MIN:
        return False
    invocador = _invocador(message) if message.author.bot else None
    autor_contador = message.webhook_id or (invocador.id if invocador else message.author.id)
    chave = (message.guild.id, autor_contador, texto[:500])
    agora = time.monotonic()
    _repetidas_chamadas += 1
    if _repetidas_chamadas >= 500:
        _repetidas_chamadas = 0
        for antiga, valores in list(_repetidas.items()):
            if not valores or agora - valores[-1][0] > settings.SPAM_MULTICANAL_JANELA_S:
                _repetidas.pop(antiga, None)
    fila = _repetidas[chave]
    while fila and agora - fila[0][0] > settings.SPAM_MULTICANAL_JANELA_S:
        fila.popleft()
    fila.append((agora, message.channel.id, message.id))
    if (len(fila) < settings.SPAM_MULTICANAL_MENSAGENS_MIN
            or len({canal for _, canal, _ in fila}) < settings.SPAM_MULTICANAL_CANAIS_MIN):
        return False
    mensagens_ataque = list(fila)
    fila.clear()
    for _, canal_id, message_id in mensagens_ataque:
        try:
            await rest.request("DELETE", f"/channels/{canal_id}/messages/{message_id}")
        except rest.DiscordHTTPError as exc:
            if exc.status != 404 and exc.codigo != 10008:
                log.warning("não consegui remover mensagem do spam multicanal: %s", exc)
        except rest.DiscordTransportError as exc:
            log.warning("falha de rede removendo mensagem do spam multicanal: %s", exc)
    if message.webhook_id:
        await _punir_webhook(message.guild, message.webhook_id,
                             "Mensagens repetidas em vários canais", message.channel)
    else:
        atencao = contexto_atencao(message.guild.id, autor_contador)
        acao_atenta = bool(atencao and atencao["usuario"] == autor_contador)
        if message.author.bot and _invocador(message) is not None:
            executor = _invocador(message)
            atencao_bot = contexto_atencao(message.guild.id, message.author.id)
            if atencao_bot and atencao_bot["confirmado"]:
                await _punir_bot_em_spam(message)
            elif not protegido(executor.id):
                if acao_atenta:
                    await punir(message.guild, executor.id,
                                f"Spam destrutivo por aplicativo após comando {atencao['comando']}")
                else:
                    await aplicar_sancao(message.guild, executor.id, "medio", "Spam por aplicativo em vários canais", categoria="aplicativo_externo")
        elif message.author.bot and getattr(message, "interaction_metadata", None) is not None:
            await _punir_bot_em_spam(message)
        elif message.author.bot:
            await aplicar_sancao(message.guild, message.author.id, "grave", "Mensagens repetidas em vários canais", categoria="spam_bot", bot_executor=True)
        else:
            if acao_atenta:
                await punir(message.guild, message.author.id,
                            f"Spam destrutivo após comando {atencao['comando']}")
            else:
                await aplicar_sancao(message.guild, message.author.id, "baixo", "Mensagens repetidas em vários canais", categoria="mensagem_multicanal")
    return True


async def mensagem_bot(message: discord.Message) -> bool:
    bot_id = message.author.id
    if protegido(bot_id):
        return False
    if await _spam_sequencial(message) or await _spam_cruzado(message):
        return True
    if not (_mencoes_em_massa(message) and estourou("mencao_msg", bot_id)):
        return False
    await _apagar(message)
    await aplicar_sancao(
        message.guild,
        bot_id,
        "grave",
        "Menções em massa por bot",
        [("Local", getattr(message.channel, "mention", str(message.channel)))],
        categoria="spam_bot",
        bot_executor=True,
    )
    return True


def _mencoes_em_massa(message: discord.Message) -> bool:
    return (
        message.mention_everyone or len(message.mentions) >= settings.RAID_MENCOES_MAX
    )


_PALAVROES_ARQ = settings.PACKAGE_DIR / "config" / "palavroes.txt"
_LINKS_ARQ = settings.PACKAGE_DIR / "config" / "links_proibidos.txt"
_NIVEL_SANCAO = {"baixo": "baixo", "medio": "medio", "alto": "grave"}


def _textos_da_mensagem(message: discord.Message) -> list[str]:
    """Só o que a pessoa escreveu e o nome dos arquivos. Embeds (preview automático de link, com título de
    notícia etc.) e URLs de CDN não entram: punir alguém por algo que não digitou seria injusto."""
    textos = [message.content or ""]
    for a in getattr(message, "attachments", None) or []:
        textos.append(a.filename or "")
    return textos


async def _link_proibido(message: discord.Message) -> bool:
    """Site proibido (lista ou imitação do Discord): apaga e aplica a escada do nível do site."""
    if not flag("links_ativo"):
        return False
    achado = links.obter(_LINKS_ARQ).achar(message.content or "")
    if achado is None:
        return False
    nivel, host = achado  # o domínio vai para o log; o conteúdo da mensagem não
    await _apagar(message)
    await aplicar_sancao(
        message.guild,
        message.author.id,
        _NIVEL_SANCAO[nivel],
        f"Site proibido (nível {nivel})",
        [("Site", host), ("Local", getattr(message.channel, "mention", str(message.channel)))],
    )
    return True


async def _palavrao(message: discord.Message) -> bool:
    if not flag("palavroes_ativo"):
        return False
    textos = _textos_da_mensagem(message)
    # OCR (opcional): texto de imagens, GIFs e vídeos. Sem chave ou desligado, não faz nada.
    textos += await ocr.textos_de_anexos(getattr(message, "attachments", None))
    filtro = palavroes.obter(_PALAVROES_ARQ)
    achado = palavroes.melhor(filtro, textos)
    if achado is None:
        # Sem certeza: não pune. Pede ao dono, no canal de logs.
        suspeito = palavroes.melhor_suspeito(filtro, textos)
        if suspeito is not None:
            await _pedir_revisao(message, suspeito[0], suspeito[1])
        return False
    nivel, _trecho = achado  # o trecho não vai para o log, para não repetir o palavrão
    await _apagar(message)
    await aplicar_sancao(
        message.guild,
        message.author.id,
        _NIVEL_SANCAO[nivel],
        f"Palavrão (nível {nivel})",
        [("Local", getattr(message.channel, "mention", str(message.channel)))],
    )
    return True


async def _pedir_revisao(message: discord.Message, nivel: str, trecho: str) -> None:
    """Pergunta ao dono sobre um trecho suspeito. A mensagem fica como está. Cada trecho é perguntado 1x por dia."""
    hoje = time.strftime("%Y-%m-%d")
    vistos = json.loads(store.get_config("revisao:vistos") or "{}")
    if vistos.get(trecho) == hoje:
        return
    vistos[trecho] = hoje
    store.set_config("revisao:vistos", json.dumps(vistos))
    rid = str(message.id)
    store.set_config(
        f"revisao:{rid}",
        json.dumps({
            "trecho": trecho,
            "nivel": nivel,
            "mensagem": message.id,
            "canal": message.channel.id,
            "guild": message.guild.id,
            "autor": message.author.id,
        }),
    )
    canal_logs = int(store.ajuste("logs_channel_id") or settings.LOGS_CHANNEL_ID or 0)
    if not canal_logs:
        log.error("sem canal de logs: não consigo pedir revisão de palavrão")
        return
    botao = lambda acao, rotulo, estilo: {"type": 2, "style": estilo, "label": rotulo, "custom_id": f"pal:{acao}:{rid}"}
    await rest.send_message(canal_logs, {
        "content": (
            f"{E.alert} **Possível palavrão (sem certeza)** · nível sugerido: {nivel}\n"
            f"Autor: <@{message.author.id}> · Canal: <#{message.channel.id}>\n"
            f"Trecho: `{trecho}`\n"
            "A mensagem continua no canal. Escolha uma opção:"
        ),
        "components": [{"type": 1, "components": [
            botao("baixo", "Punir: baixo", 2),
            botao("medio", "Punir: médio", 2),
            botao("alto", "Punir: alto", 2),
            botao("nao", "Não é palavrão", 3),
            botao("ignorar", "Ignorar", 2),
        ]}],
    })


async def punir_revisado(guild: discord.Guild, dados: dict, nivel: str) -> str:
    """Dono confirmou: apaga a mensagem e aplica a escada do nível escolhido."""
    try:
        await rest.request("DELETE", f"/channels/{dados['canal']}/messages/{dados['mensagem']}")
    except (rest.DiscordHTTPError, rest.DiscordTransportError):
        pass
    await aplicar_sancao(
        guild,
        dados["autor"],
        _NIVEL_SANCAO[nivel],
        f"Palavrão confirmado pelo dono (nível {nivel})",
        [("Local", f"<#{dados['canal']}>")],
    )
    return f"Mensagem apagada e punição aplicada (nível {nivel})."


async def mensagem_humana(message: discord.Message) -> bool:
    autor = message.author.id
    if await _link_proibido(message):
        return True
    if await _palavrao(message):
        return True
    if await _spam_sequencial(message) or await _spam_cruzado(message):
        return True
    if not (_mencoes_em_massa(message) and estourou("mencao_msg", autor)):
        return False
    await _apagar(message)
    await aplicar_sancao(
        message.guild,
        autor,
        "medio",
        "Menções em massa",
        [("Local", getattr(message.channel, "mention", str(message.channel)))],
        categoria="mencoes_massa",
    )
    return True


# ── Cargos, convites, bans, expulsões e emojis ───────────────────────────────


async def _varrer_auditoria(guild, acao, tipo: str, motivo: str, apos=None) -> None:
    if not ligado():
        return
    for entrada in await _entradas_novas(guild, acao):
        if excedeu(tipo, entrada.user):
            if apos is not None:
                await apos(entrada)
            await punir(guild, entrada.user.id, motivo)


async def cargo_criado(role: discord.Role) -> None:
    await _varrer_auditoria(
        role.guild,
        discord.AuditLogAction.role_create,
        "role_create",
        "Criação de cargos em massa",
    )


async def cargo_removido(role: discord.Role) -> None:
    await _varrer_auditoria(
        role.guild,
        discord.AuditLogAction.role_delete,
        "role_delete",
        "Exclusão de cargos em massa",
    )


async def emojis_criados(guild: discord.Guild) -> None:
    async def apagar_emoji(entrada) -> None:
        emoji = discord.utils.get(guild.emojis, id=getattr(entrada.target, "id", None))
        if emoji is not None:
            await _apagar(emoji)

    await _varrer_auditoria(
        guild,
        discord.AuditLogAction.emoji_create,
        "emoji_create",
        "Criação de emojis em massa",
        apos=apagar_emoji,
    )


async def convite_criado(invite: discord.Invite) -> None:
    guild = invite.guild
    if guild is None:
        return
    creator_id = getattr(invite.inviter, "id", None)
    if creator_id is None or not await _criador_presente(guild, creator_id):
        await _apagar(invite)
        log.info("convite %s apagado: criador ausente ou não verificável", invite.code)
        return
    if not ligado():
        return
    if incidente.ativo(invite.guild.id):
        await _apagar(invite)
        return
    if excedeu("invite_create", invite.inviter):
        await _apagar(invite)
        await punir(invite.guild, invite.inviter.id, "Criação de convites em massa")


async def _criador_presente(guild: discord.Guild, uid: int) -> bool:
    """Confirma que o autor do convite ainda é membro; cache miss não significa ausência."""
    if guild.get_member(int(uid)) is not None:
        return True
    try:
        await guild.fetch_member(int(uid))
        return True
    except discord.NotFound:
        return False
    except discord.HTTPException as exc:
        # Falha fechada: não manter convite quando não foi possível validar a origem.
        log.warning("não consegui verificar criador %s do convite: %s", uid, exc)
        return False


async def banido(guild: discord.Guild, user: discord.abc.User) -> None:
    if not ligado():
        return
    autor = await autor_recente(guild, discord.AuditLogAction.ban, user.id)
    if excedeu("ban_kick", autor):
        await punir(guild, autor.id, "Bans em massa")


async def expulso(member: discord.Member) -> None:
    if not ligado():
        return
    autor = await autor_recente(member.guild, discord.AuditLogAction.kick, member.id)
    if excedeu("ban_kick", autor):
        await punir(member.guild, autor.id, "Expulsões em massa")


async def auditoria_extra(entrada):
    """Eventos sem callback específico e resposta imediata a mudanças durante contenção."""
    _guardar_autor_auditoria(entrada)
    try:
        guild, autor = entrada.guild, entrada.user
        alvo = entrada.target
        action = entrada.action
    except (TypeError, ValueError, AttributeError, KeyError) as exc:
        log.debug("entrada de auditoria parcial ignorada: %s", type(exc).__name__)
        return
    if autor is None or protegido(autor.id) or not ligado():
        return
    nome = action.name
    alvo_id = getattr(alvo, "id", None)
    if nome == "bot_add" and alvo_id:
        store.save_bot_adder(alvo_id, autor.id)
    if nome == "webhook_create" and alvo_id:
        store.save_webhook_creator(str(alvo_id), autor.id)
    extras = {
        "role_update": "role_update",
        "member_role_update": "member_role_update",
        "thread_update": "thread_update",
        "thread_delete": "thread_delete",
        "webhook_update": "webhook_update",
        "webhook_delete": "webhook_delete",
        "guild_update": "guild_update",
        "overwrite_create": "channel_update",
        "overwrite_update": "channel_update",
        "overwrite_delete": "channel_update",
        "emoji_update": "emoji_update",
        "emoji_delete": "emoji_delete",
        "sticker_create": "sticker_create",
        "sticker_update": "sticker_update",
        "sticker_delete": "sticker_delete",
        "integration_create": "integration_create",
        "integration_update": "integration_update",
        "integration_delete": "integration_delete",
        "automod_rule_create": "automod_rule_create",
        "automod_rule_update": "automod_rule_update",
        "automod_rule_delete": "automod_rule_delete",
        "unban": "unban",
        "member_prune": "member_prune",
        "message_bulk_delete": "message_bulk_delete",
        "stage_instance_create": "stage_instance_create",
        "stage_instance_delete": "stage_instance_delete",
        "stage_instance_update": "stage_instance_update",
        "member_update": "member_update",
        "message_delete": "message_delete",
        "scheduled_event_create": "scheduled_event_create",
        "scheduled_event_update": "scheduled_event_update",
        "scheduled_event_delete": "scheduled_event_delete",
        "soundboard_sound_create": "soundboard_sound_create",
        "soundboard_sound_update": "soundboard_sound_update",
        "soundboard_sound_delete": "soundboard_sound_delete",
    }
    ativos = set(extras) | {
        "channel_create",
        "channel_delete",
        "channel_update",
        "role_create",
        "role_delete",
        "webhook_create",
        "thread_create",
        "invite_create",
        "bot_add",
        "emoji_create",
        "kick",
        "ban",
    }
    if nome not in ativos:
        return
    acoes_estruturais = {
        "channel_create",
        "channel_delete",
        "channel_update",
        "role_create",
        "role_delete",
        "role_update",
        "member_role_update",
        "thread_create",
        "thread_update",
        "thread_delete",
        "webhook_create",
        "webhook_update",
        "webhook_delete",
        "guild_update",
        "overwrite_create",
        "overwrite_update",
        "overwrite_delete",
        "emoji_create",
        "emoji_update",
        "emoji_delete",
        "sticker_create",
        "sticker_update",
        "sticker_delete",
        "automod_rule_create",
        "automod_rule_update",
        "automod_rule_delete",
        "stage_instance_create",
        "stage_instance_update",
        "stage_instance_delete",
        "scheduled_event_create",
        "scheduled_event_update",
        "scheduled_event_delete",
        "message_bulk_delete",
        "member_prune",
        "kick",
        "ban",
        "unban",
    }
    atencao = (
        await conter_acao_em_atencao(guild, autor, nome)
        if nome in acoes_estruturais else False
    )
    if (
        atencao
        or incidente.ativo(guild.id)
        or (nome in extras and estourou(extras[nome], autor.id))
    ):
        if not atencao:
            await punir(
                guild,
                autor.id,
                f"Alteração destrutiva/abusiva: {nome}",
                [("Alvo", alvo_id or "servidor")],
            )
        if nome.startswith("webhook") and alvo_id:
            await apagar_webhook(alvo_id)
