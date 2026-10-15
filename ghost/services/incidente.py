"""Contenção persistente. Só libera após silêncio, sanções e restauração sem avisos.

Atribuição por convite é inferência por contadores, nunca prova de autoria.
Uma entrada ambígua é registrada, mas não recebe banimento por associação.
"""

import asyncio
import json
import logging
import time
from collections import defaultdict
from datetime import datetime, timedelta

import discord

from ..config import settings
from ..core import diario
from ..data import store

log = logging.getLogger(__name__)
_estados = {}
_tarefas = {}
_locks = defaultdict(asyncio.Lock)
_convites = {}
_join_locks = defaultdict(asyncio.Lock)
_join_pending = defaultdict(int)
_replay = set()  # webhooks criados pelo restaurador, jamais só pelo nome
_exclusao_lock = asyncio.Semaphore(settings.INCIDENTE_CONCORRENCIA_EXCLUSOES)
QUIETO_S = settings.INCIDENTE_SILENCIO_S
RETROATIVO_S = settings.INCIDENTE_RETROSPECTIVA_HORAS * 3600


def estado(guild_id):
    if guild_id not in _estados:
        raw = store.get_config(f"incidente:{guild_id}")
        _estados[guild_id] = json.loads(raw) if raw else {}
    return _estados[guild_id]


def salvar(guild_id):
    store.set_config(f"incidente:{guild_id}", json.dumps(estado(guild_id)))


def ativo(guild_id=None):
    return bool(estado(guild_id or settings.GUILD_ID).get("ativo"))


def alvo(guild_id, uid):
    return str(uid) in estado(guild_id).get("alvos", {})


def dono(guild, uid):
    return uid == guild.owner_id


def atividade(guild):
    if ativo(guild.id):
        estado(guild.id)["ultima"] = time.time()
        salvar(guild.id)


_ultimas_sancoes: dict[tuple, float] = {}
SANCAO_REPETIDA_S = 10


def registrar(guild, evento, **dados):
    if evento == "Cadeia de entrada":
        # A mesma cadeia é reanalisada a cada exclusão; só uma mudança real gera linha nova.
        cadeias = estado(guild.id).setdefault("cadeias", {})
        chave = str(dados["alvo"])
        if cadeias.get(chave) == dados:
            return
        cadeias[chave] = dados
        salvar(guild.id)
    elif evento == "Sanção":
        # A mesma sanção pode ser aplicada por dois caminhos em sequência; registra uma vez.
        assinatura = (
            guild.id,
            str(dados.get("alvo")),
            dados.get("ok"),
            dados.get("pena"),
            dados.get("motivo"),
        )
        agora = time.monotonic()
        if agora - _ultimas_sancoes.get(assinatura, float("-inf")) < SANCAO_REPETIDA_S:
            return
        _ultimas_sancoes[assinatura] = agora
    diario.evento("raid_incidente", evento, [(k, str(v)) for k, v in dados.items()])
    log.info("%s guild=%s %s", evento, guild.id, dados)


async def iniciar(guild, motivo, *, tentar_novamente=False):
    from . import backup

    s = estado(guild.id)
    if tentar_novamente:
        s.pop("erro", None)
    if not s.get("ativo"):
        confiavel = backup.ultimo_valido()
        s.update(
            ativo=True,
            fase="contencao",
            inicio=time.time(),
            ultima=time.time(),
            backup=confiavel[0].name if confiavel else None,
            convites_antes="INVITES_DISABLED" in guild.features,
            pausa_temporaria_antes=(
                guild.invites_paused_until.isoformat()
                if getattr(guild, "invites_paused_until", None)
                else None
            ),
            pausa_ok=False,
            alvos={},
            adicionadores_contidos=[],
            revogar_convites=[],
            falhas=[],
            avisados=[],
            mapa={},
        )
        salvar(guild.id)  # persistir ANTES da primeira chamada de rede
        registrar(guild, "Incidente iniciado", motivo=motivo)
    else:
        atividade(guild)
    if guild.id not in _tarefas or _tarefas[guild.id].done():
        _tarefas[guild.id] = asyncio.create_task(
            _acompanhar(guild), name=f"raid-{guild.id}"
        )


async def pausar(guild):
    s = estado(guild.id)
    try:
        try:
            await guild.edit(
                invites_disabled=True, reason="Anti-raid: convites pausados"
            )
            s["modo_pausa"] = "permanente"
        except discord.HTTPException:
            await guild.edit(
                invites_disabled_until=discord.utils.utcnow() + timedelta(hours=24),
                reason="Anti-raid: pausa temporária renovável",
            )
            s["modo_pausa"] = "temporaria"
        s.update(pausa_ok=True, pausa_renovada=time.time())
    except discord.HTTPException as exc:
        s["pausa_ok"] = False
        registrar(
            guild,
            "Falha ao pausar convites; proteção permanece ativa",
            erro=type(exc).__name__,
        )
    salvar(guild.id)


async def registrar_convites(guild):
    try:
        convites = await guild.invites()
    except discord.HTTPException:
        _convites.pop(guild.id, None)
        return
    _convites[guild.id] = {
        i.code: {"usos": i.uses or 0, "criador": getattr(i.inviter, "id", None)}
        for i in convites
    }


async def entrada(member):
    """Serializa leituras; lotes concorrentes ou múltiplos deltas são ambíguos."""
    guild = member.guild
    from . import raid

    if raid.estourou("member_join", guild.id):
        await iniciar(guild, "Entrada de membros em massa")
    _join_pending[guild.id] += 1
    try:
        async with _join_locks[guild.id]:
            antes = _convites.get(guild.id)
            await registrar_convites(guild)
            depois = _convites.get(guild.id)
            codigo = criador = None
            if antes is not None and depois is not None and not member.bot:
                deltas = [
                    (code, item)
                    for code, item in depois.items()
                    if code in antes and item["usos"] > antes[code]["usos"]
                ]
                if (
                    len(deltas) == 1
                    and _join_pending[guild.id] == 1
                    and deltas[0][1]["usos"] - antes[deltas[0][0]]["usos"] == 1
                ):
                    codigo, item = deltas[0]
                    criador = item["criador"]
            from ..database.connection import connection

            with connection() as conn:
                conn.execute(
                    "INSERT INTO raid_entradas (guild_id,user_id,entrou,convite,criador) VALUES (?,?,?,?,?)",
                    (
                        str(guild.id),
                        str(member.id),
                        time.time(),
                        codigo,
                        str(criador) if criador else None,
                    ),
                )
            registrar(
                guild,
                "Entrada registrada",
                membro=member.id,
                convite=codigo or "indeterminado",
                criador=criador or "indeterminado",
                evidencia="inferência por uso único" if codigo else "ambígua/ausente",
            )
    finally:
        _join_pending[guild.id] -= 1
    if ativo(guild.id):
        # Entrada durante contenção não prova participação no ataque.
        # O chamador impede cargos automáticos até o incidente terminar.
        return True
    return False


def origem(guild_id, uid):
    from ..database.connection import connection

    with connection() as conn:
        row = conn.execute(
            "SELECT convite,criador,entrou FROM raid_entradas WHERE guild_id=? AND user_id=? ORDER BY entrou DESC LIMIT 1",
            (str(guild_id), str(uid)),
        ).fetchone()
    return dict(row) if row else {}


async def vinculos(guild, uid):
    """Revoga convites relacionados; pune entradas recentes de códigos atribuídos sem ambiguidade."""
    from . import raid

    adder = store.bot_adder(uid)
    pessoa = int(adder) if adder else uid
    entrada_pessoa = origem(guild.id, pessoa)
    criador = entrada_pessoa.get("criador")
    criadores = {uid}
    s = estado(guild.id)
    s["revogar_convites"] = sorted(set(s.get("revogar_convites", [])) | criadores)
    salvar(guild.id)
    registrar(
        guild,
        "Cadeia de entrada",
        alvo=uid,
        adicionou_bot=adder or "desconhecido",
        pessoa=pessoa,
        convidou_pessoa=criador or "desconhecido",
        convite=entrada_pessoa.get("convite"),
        nota="criador do convite não prova quem compartilhou nem participação na raid",
    )
    try:
        for invite in await guild.invites():
            if getattr(invite.inviter, "id", None) in criadores:
                await invite.delete(reason="Anti-raid: convite vinculado ao incidente")
    except discord.HTTPException as exc:
        registrar(guild, "Revogação de convite falhou", erro=type(exc).__name__)
    if adder and not raid.protegido(int(adder)):
        # Bot-add auditado é associação, não prova de autoria das ações do bot.
        await raid.punir_adicionador(guild, int(adder))


async def sancionar(guild, uid, motivo, *, banir=False, reaplicar=False):
    from . import raid

    uid = int(uid)
    if dono(guild, uid) or uid == raid._bot_id():
        return False
    async with _locks[(guild.id, uid)]:
        s = estado(guild.id)
        registro = s.setdefault("alvos", {}).setdefault(str(uid), {})
        if (
            registro.get("ok")
            and not reaplicar
            and (registro.get("pena") == "ban" or not banir)
        ):
            return True
        registro.update(ok=False, motivo=motivo, pena="ban" if banir else "quarentena")
        salvar(guild.id)
        try:
            try:
                member = guild.get_member(uid) or await guild.fetch_member(uid)
            except discord.NotFound:
                member = None
            if member is not None:
                registro.setdefault("cargos_antes", [r.id for r in member.roles])
                # Um bot executor é banido: timeout não se aplica a bots.
                banir = banir or member.bot
                if not banir:
                    await raid._tirar_cargos(guild, uid, motivo)
                    atualizado = await guild.fetch_member(uid)
                    await atualizado.timeout(timedelta(hours=1), reason=motivo)
            if banir:
                await guild.ban(
                    discord.Object(id=uid), reason=motivo,
                    delete_message_seconds=raid.BAN_LIMPAR_MENSAGENS_S,
                )
                await raid.avisar_ban(guild, uid, motivo)
            registro.update(ok=True, pena="ban" if banir else "quarentena")
        except discord.HTTPException as exc:
            # O alvo já foi atribuído por evidência de ação, não por convite.
            try:
                await guild.ban(
                    discord.Object(id=uid),
                    reason=f"Contenção alternativa: {motivo}",
                    delete_message_seconds=raid.BAN_LIMPAR_MENSAGENS_S,
                )
                await raid.avisar_ban(guild, uid, f"Contenção alternativa: {motivo}")
                registro.update(ok=True, pena="ban")
            except discord.HTTPException:
                registrar(
                    guild,
                    "Não foi possível neutralizar alvo",
                    alvo=uid,
                    erro=type(exc).__name__,
                )
        salvar(guild.id)
        registrar(guild, "Sanção", alvo=uid, **registro)
        return registro["ok"]


def _sem_permissao_para_apagar(guild, canal) -> bool:
    me = guild.me
    if me is None or not hasattr(canal, "permissions_for"):
        return False
    return not canal.permissions_for(me).manage_messages


def _falha_de_exclusao(guild, s, message, erro: str) -> None:
    """Cada falha continua bloqueando a liberação; o log registra cada canal/erro só uma vez por incidente."""
    s.setdefault("falhas", []).append(f"Mensagem não apagada: {message.id} ({erro})")
    s["falhas"] = s["falhas"][-50:]
    chave = f"{message.channel.id}:{erro}"
    avisados = s.setdefault("avisados", [])
    if chave not in avisados:
        avisados.append(chave)
        registrar(
            guild,
            "Falha na exclusão durante contenção",
            canal=message.channel.id,
            erro=erro,
        )
    salvar(guild.id)


async def filtrar(message):
    guild = message.guild
    if guild is None or not ativo(guild.id) or dono(guild, message.author.id):
        return False
    from . import raid
    if message.author.id == raid._bot_id() and not message.webhook_id:
        return False  # confirmações, vitrines e logs não pertencem ao tráfego hostil
    s = estado(guild.id)
    # Durante a fase final só o webhook explicitamente registrado pelo restaurador pode repor o snapshot.
    if s.get("fase") == "restauracao" and message.webhook_id in _replay:
        return True  # não executar comandos do histórico
    if _sem_permissao_para_apagar(guild, message.channel):
        _falha_de_exclusao(guild, s, message, "sem permissão Gerenciar Mensagens no canal")
    else:
        async with _exclusao_lock:
            try:
                await message.delete()
            except discord.NotFound:
                pass
            except discord.HTTPException as exc:
                _falha_de_exclusao(guild, s, message, type(exc).__name__)
    # Mensagens também renovam o silêncio, exceto logs internos que não podem formar um loop.
    if message.author.id != raid._bot_id():
        atividade(guild)
        if message.webhook_id:
            await raid.mensagem_webhook(message)
        elif message.author.bot:
            await raid.mensagem_bot(message)
        elif not raid.protegido(message.author.id):
            await raid.mensagem_humana(message)
    return True


async def _acompanhar(guild):
    # Mesmo com restauração pendente, mantém a pausa renovada e tenta sanções falhas.
    while ativo(guild.id):
        await _ciclo(guild)
        if not ativo(guild.id):
            return
        await asyncio.sleep(30)


async def _ciclo(guild):
    from . import backup

    try:
        await pausar(guild)
        while ativo(guild.id):
            await asyncio.sleep(5)
            s = estado(guild.id)
            if not s.get("pausa_ok") or (
                s.get("modo_pausa") == "temporaria"
                and time.time() - s.get("pausa_renovada", 0) > 3600
            ):
                await pausar(guild)
                continue
            if time.time() - s["ultima"] < QUIETO_S:
                continue
            for uid, pena in list(s.get("alvos", {}).items()):
                if not pena.get("ok"):
                    await sancionar(
                        guild, int(uid), pena["motivo"], banir=pena["pena"] == "ban"
                    )
            if any(not p.get("ok") for p in s.get("alvos", {}).values()):
                continue
            restauracao = backup.estado_restauracao()
            # Uma nova atividade suspeita depois de uma pausa/falha durante
            # uma restauração deve reativar a reconciliação automaticamente.
            # Sem atividade nova, respeitamos o comando explícito de parar.
            if (
                restauracao.get("status") in {"parado", "erro"}
                and restauracao.get("arquivo") == s.get("backup")
            ):
                marco = restauracao.get(
                    "pausado_em" if restauracao.get("status") == "parado" else "erro_em",
                    float("inf"),
                )
                if float(s.get("ultima", 0)) > float(marco):
                    retomada = await backup.continuar(guild)
                    if retomada.startswith("Continuando"):
                        s.pop("erro", None)
                        s["fase"] = "restauracao"
                        salvar(guild.id)
                        log.warning("nova atividade durante incidente; retomando restore após pausa/falha")
                        continue
                    log.warning("nova atividade detectada, mas restore não retomou: %s", retomada)
            if s.get("erro"):
                # A versão anterior guardava o pedido de confirmação junto de
                # erros já resolvidos pelo retry. Uma limpeza retomada tem
                # estado próprio; deixa a nova tentativa provar se ainda falha.
                if restauracao.get("status") == "aguardando":
                    s.pop("erro", None)
                    salvar(guild.id)
                else:
                    return
            if not s.get("backup"):
                s["erro"] = "Sem snapshot estrutural válido anterior. Proteção mantida; dono deve resolver."
                salvar(guild.id)
                return
            restauracao = backup.estado_restauracao()
            if restauracao.get("status") == "rodando":
                continue  # o processo retomado no boot detém a restauração
            if restauracao.get("status") == "aguardando":
                # Só o replay do histórico comum depende da escolha do dono.
                # Estrutura, contenção e liberação prosseguem mesmo se alguma
                # limpeza parcial falhou; ela já está registrada como aviso.
                resultado = {"feitos": ["estrutura restaurada; histórico comum aguarda escolha do dono"], "aguardando": True}
            else:
                resultado = None
            if restauracao.get("status") == "erro" and restauracao.get("arquivo") == s["backup"]:
                s["erro"] = "Restore pendente com falhas; corrija e use /restauracao continuar."
                salvar(guild.id)
                return
            if resultado is None:
                dados = backup._carregar_backup(s["backup"])
                marco = s["ultima"]
                s["falhas"] = []
                s["fase"] = "restauracao"
                salvar(guild.id)
                resultado = await backup.restaurar(
                    guild, dados, motivo="raid neutralizada", arquivo=s["backup"]
                )
                _replay.clear()
                s["fase"] = "contencao"
                salvar(guild.id)
            else:
                marco = s["ultima"]
            # Só uma interrupção real ou falha de contenção mantém a proteção.
            # O restore termina em concluido_com_avisos quando itens legados,
            # vitrines ou mensagens não são recuperáveis; isso é terminal e
            # nunca deve provocar replay infinito nem bloquear a liberação.
            if (
                resultado.get("erro")
                or resultado.get("pausado")
                or s.get("falhas")
            ):
                s["erro"] = {**resultado, "contencao": s.get("falhas", [])}
                salvar(guild.id)
                registrar(
                    guild,
                    "Restauração incompleta; proteção mantida",
                    resultado=resultado,
                )
                return  # evita repetir reenvios destrutivos a cada cinco segundos
            for uid, pena in list(s.get("alvos", {}).items()):
                await sancionar(
                    guild,
                    int(uid),
                    pena["motivo"],
                    banir=pena["pena"] == "ban",
                    reaplicar=True,
                )
            if s["ultima"] != marco or any(
                not p.get("ok") for p in s["alvos"].values()
            ):
                continue
            s["fase"] = "liberacao"
            salvar(guild.id)
            if s.get("modo_pausa") == "temporaria":
                antigo = s.get("pausa_temporaria_antes")
                await guild.edit(
                    invites_disabled_until=datetime.fromisoformat(antigo)
                    if antigo
                    else None,
                    reason="Anti-raid: restauração concluída",
                )
            else:
                await guild.edit(
                    invites_disabled=s["convites_antes"],
                    reason="Anti-raid: restauração concluída",
                )
            if s["ultima"] != marco:
                s["fase"] = "contencao"
                await pausar(guild)
                continue
            s.update(ativo=False, fase="concluido", fim=time.time())
            salvar(guild.id)
            from . import notify

            await notify.registrar(
                "raid_punicao",
                "Raid contida e restauração concluída",
                settings.CORES["info"],
                [
                    ("Alvos", json.dumps(s["alvos"])[:900]),
                    ("Convites", "estado anterior reposto"),
                    (
                        "Restore",
                        "concluído com avisos: " + "; ".join(resultado.get("avisos", []))[:700]
                        if resultado.get("avisos") else "concluído",
                    ),
                ],
            )
            for cadeia in s.get("cadeias", {}).values():
                await notify.registrar(
                    "raid_punicao",
                    "Cadeia de entrada da raid",
                    settings.CORES["info"],
                    [(k, str(v)) for k, v in cadeia.items()],
                )
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("Incidente interrompido; estado de proteção preservado")
        estado(guild.id)["fase"] = "contencao"
        estado(guild.id)["erro"] = (
            "Falha inesperada. Consulte o diário e use raidretomar após corrigir."
        )
        salvar(guild.id)
    finally:
        _replay.clear()


async def retomar(guild):
    await registrar_convites(guild)
    if ativo(guild.id):
        from . import backup

        restauracao = backup.estado_restauracao()
        if s_erro := estado(guild.id).get("erro"):
            if restauracao.get("status") in {"rodando", "aguardando"}:
                estado(guild.id).pop("erro", None)
                estado(guild.id)["fase"] = "contencao"
                salvar(guild.id)
                log.warning(
                    "erro antigo do incidente reavaliado durante retomada do restore (%s)",
                    type(s_erro).__name__,
                )
        await iniciar(guild, "Retomada após reinício")


async def close():
    for task in _tarefas.values():
        task.cancel()
    await asyncio.gather(*_tarefas.values(), return_exceptions=True)
