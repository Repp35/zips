"""Arquiva mensagens para restauração e gera snapshots completos por backup.

No snapshot, inclui mensagens de membros, bots e webhooks em canais e threads legíveis.
Na restauração, substitui as mensagens atuais e reapresenta o nome/foto e a hora original
com webhooks, mantendo conteúdos, embeds e componentes quando a API aceita o payload.
O arquivo incremental em JSONL permanece para compatibilidade com backups antigos.
"""

import asyncio
import json
import logging
import os
import tempfile
from collections import defaultdict, deque
from datetime import timedelta
from pathlib import Path

import discord

from ..data import store
from ..config import settings
from ..core.files import atomic_text
from . import context, regras, rest, reconciliar

log = logging.getLogger("mensagens")

PASTA: Path = settings.BASE_DIR / "backups" / "mensagens"
AVATARES: Path = settings.BASE_DIR / "backups" / "avatares"
CURSORES = PASTA / "_cursores.json"
INDICE_AVATARES = AVATARES / "_indice.json"
NOME_WEBHOOK = "Ghost backup"
FLAG_COMPONENTES_V2 = 1 << 15
ANEXOS: Path = settings.BASE_DIR / "backups" / "anexos"  # arquivos baixados no momento do backup


# ── Fotos ─────────────────────────────────────────────────────────────────────


def _ler_json(caminho: Path, padrao):
    try:
        return json.loads(caminho.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return padrao


async def atualizar_avatares(guild: discord.Guild) -> int:
    """Guarda a foto atual de cada membro humano (uma vez por dia). Reaproveita a foto se não mudou."""
    AVATARES.mkdir(parents=True, exist_ok=True)
    indice = _ler_json(INDICE_AVATARES, {})
    http = await rest._http()
    baixadas = 0
    try:
        if not guild.chunked:
            await guild.chunk()
    except (discord.HTTPException, discord.ClientException):
        log.warning("lista de membros indisponível: fotos podem faltar")
    for membro in guild.members:
        if membro.bot:
            continue
        uid = str(membro.id)
        atual = indice.get(uid)
        url = str(membro.display_avatar.url)
        chave = membro.display_avatar.key
        if (
            atual
            and atual.get("chave") == chave
            and (AVATARES / atual["arquivo"]).exists()
        ):
            atual["url"] = url
            continue
        try:
            async with http.get(url) as resp:
                if resp.status != 200:
                    continue
                dados = await resp.read()
        except Exception:
            log.warning("foto de %s não baixada", uid)
            continue
        extensao = "gif" if str(chave).startswith("a_") else "png"
        arquivo = f"{uid}_{chave}.{extensao}"
        (AVATARES / arquivo).write_bytes(dados)
        indice[uid] = {"chave": chave, "arquivo": arquivo, "url": url}
        baixadas += 1
    atomic_text(INDICE_AVATARES, json.dumps(indice, ensure_ascii=False))
    return baixadas


# ── Arquivo ───────────────────────────────────────────────────────────────────


def _canais_do_arquivo(guild: discord.Guild) -> list[int]:
    ids = [c.id for c in guild.text_channels]
    ids += [t.id for t in guild.threads]
    return ids


def _ultimo_id_arquivado(canal_id: int) -> str | None:
    """Lê só a última linha do JSONL para retomar com segurança após queda."""
    caminho = PASTA / f"{canal_id}.jsonl"
    try:
        with caminho.open("rb") as arquivo:
            arquivo.seek(0, 2)
            fim = arquivo.tell()
            if fim == 0:
                return None
            pos = fim - 1
            while pos > 0:
                arquivo.seek(pos - 1)
                if arquivo.read(1) == b"\n":
                    break
                pos -= 1
            arquivo.seek(pos)
            ultima = arquivo.readline().decode("utf-8").strip()
        registro = json.loads(ultima)
        return str(registro["id"])
    except (OSError, ValueError, KeyError, TypeError, UnicodeError):
        return None


async def _buscar(canal_id: int, cursor: str | None, *, progresso=None) -> list[dict]:
    """Todas as mensagens novas do canal, sem limite de quantidade."""
    saida: list[dict] = []
    if cursor is None:
        antes = None
        while True:
            qs = "?limit=100" + (f"&before={antes}" if antes else "")
            lote = await rest.request("GET", f"/channels/{canal_id}/messages{qs}") or []
            if not lote:
                break
            saida.extend(lote)
            if progresso:
                progresso(len(saida))
            proximo = min(int(m["id"]) for m in lote)
            if antes is not None and proximo >= int(antes):
                raise RuntimeError(
                    f"paginação do histórico do canal {canal_id} não avançou"
                )
            antes = proximo
            if len(lote) < 100:
                break
    else:
        cursor_id = int(cursor)
        antes = None
        while True:
            qs = (
                f"?limit=100&after={cursor_id}"
                if antes is None
                else f"?limit=100&before={antes}"
            )
            lote = (
                await rest.request("GET", f"/channels/{canal_id}/messages{qs}")
                or []
            )
            if not lote:
                break
            ids_lote = [int(m["id"]) for m in lote]
            proximo = min(ids_lote)
            if antes is not None and proximo >= int(antes):
                raise RuntimeError(
                    f"paginação incremental do canal {canal_id} não avançou"
                )
            saida.extend(m for m in lote if int(m["id"]) > cursor_id)
            if progresso:
                progresso(len(saida))
            if proximo <= cursor_id or len(lote) < 100:
                break
            antes = proximo
    return saida


def _registro(m: dict, canal_id: int, indice: dict) -> dict:
    autor = m.get("author") or {}
    uid = autor.get("id")
    membro = m.get("member") or {}
    foto = indice.get(str(uid)) or {}
    return {
        "id": m["id"],
        "canal": str(canal_id),
        "data": m.get("timestamp"),
        "autor": {
            "id": uid,
            "usuario": autor.get("username"),
            "nome_publico": autor.get("global_name"),
            "apelido": membro.get("nick"),
            "foto_arquivo": foto.get("arquivo"),
            "foto_url": foto.get("url")
            or (
                f"https://cdn.discordapp.com/avatars/{uid}/{autor['avatar']}.png"
                if autor.get("avatar")
                else None
            ),
        },
        "conteudo": m.get("content") or "",
        "embeds": m.get("embeds") or [],
        "componentes": m.get("components") or [],
        "flags": m.get("flags") or 0,
        "anexos": [
            {
                "id": str(a.get("id")),
                "nome": a.get("filename"),
                "url": a.get("url"),
                "tipo": a.get("content_type"),
                "tamanho": a.get("size") or 0,
                "arquivo": None,
            }
            for a in m.get("attachments") or []
        ],
    }


async def snapshot(guild: discord.Guild) -> dict:
    """Captura o histórico acessível inteiro; incompleto significa que a restauração falha fechado."""
    ids = {c.id for c in guild.text_channels}
    ids.update(t.id for t in guild.threads)
    falhas: list[str] = []
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
                    ids.add(thread.id)
            except (discord.HTTPException, TypeError) as exc:
                falhas.append(f"threads:{canal.id}")
                log.warning(
                    "threads arquivadas de %s não capturadas: %s", canal.id, exc
                )
    indice = _ler_json(INDICE_AVATARES, {})
    canais: dict[str, list[dict]] = {}
    canais_ordenados = sorted(ids)
    mensagens_lidas = 0
    log.info("captura de histórico iniciada: %d canais/tópicos", len(canais_ordenados))
    concorrencia = asyncio.Semaphore(3)
    concluidos = 0

    async def capturar_canal(indice_canal: int, canal_id: int) -> None:
        nonlocal mensagens_lidas, concluidos
        async with concorrencia:
            def progresso(qtd, _canal=canal_id):
                if qtd and qtd % 1000 == 0:
                    log.info("captura de histórico: canal %s chegou a %d mensagens", _canal, qtd)
            try:
                lote = await _buscar(canal_id, None, progresso=progresso)
            except (rest.DiscordHTTPError, rest.DiscordTransportError, RuntimeError) as exc:
                falhas.append(str(canal_id))
                log.warning("histórico do canal %s não foi capturado: %s", canal_id, exc)
                lote = None
            except Exception:
                falhas.append(str(canal_id))
                log.exception("erro inesperado ao capturar histórico do canal %s", canal_id)
                lote = None
            if lote is not None:
                lote.sort(key=lambda m: int(m["id"]))
                mensagens_lidas += len(lote)
                # O snapshot inclui mensagens de membros, bots, webhooks e do próprio Ghost.
                registros = [_registro(m, canal_id, indice) for m in lote]
                for registro in registros:
                    for anexo in registro["anexos"]:
                        try:
                            anexo["arquivo"] = await _guardar_anexo(anexo, guild.filesize_limit)
                        except Exception:
                            anexo["arquivo"] = None
                            log.exception(
                                "erro ao guardar mídia da mensagem %s no canal %s",
                                registro["id"], canal_id,
                            )
                        if not anexo["arquivo"]:
                            falhas.append(f"anexo:{canal_id}:{registro['id']}")
                canais[str(canal_id)] = registros
            concluidos += 1
            if concluidos % 10 == 0 or concluidos == len(canais_ordenados):
                log.info(
                    "captura de histórico: %d/%d canais; %d mensagens lidas",
                    concluidos, len(canais_ordenados), mensagens_lidas,
                )

    await asyncio.gather(*(
        capturar_canal(i, canal_id)
        for i, canal_id in enumerate(canais_ordenados, 1)
    ))
    return {"completo": not falhas, "canais": canais, "falhas": falhas}


async def _guardar_anexo(a: dict, limite: int) -> str | None:
    """Baixa o anexo agora, enquanto o link ainda vale. Sem isso, a mensagem apagada perde o arquivo."""
    if not a.get("url") or not a.get("id"):
        return None
    ANEXOS.mkdir(parents=True, exist_ok=True)
    nome_seguro = "".join(c for c in (a.get("nome") or "arquivo") if c.isalnum() or c in "._-")[:80]
    destino = ANEXOS / f"{a['id']}__{nome_seguro or 'arquivo'}"
    if destino.exists():
        return str(destino.relative_to(settings.BASE_DIR))
    dados = await rest.baixar_bytes(a["url"], limite)
    if dados is None:
        log.warning("anexo %s (%s) não pôde ser guardado no backup", a["id"], a.get("nome"))
        return None
    fd, temporario = tempfile.mkstemp(prefix=".anexo-", dir=ANEXOS)
    try:
        with os.fdopen(fd, "wb") as arquivo:
            arquivo.write(dados)
            arquivo.flush()
            os.fsync(arquivo.fileno())
        os.replace(temporario, destino)
    finally:
        if os.path.exists(temporario):
            os.unlink(temporario)
    return str(destino.relative_to(settings.BASE_DIR))


def _arquivo_anexo(valor: str | None) -> Path | None:
    if not valor:
        return None
    caminho = Path(valor)
    if not caminho.is_absolute():
        caminho = settings.BASE_DIR / caminho
    if caminho.is_file():
        return caminho
    # Compatibilidade com snapshots feitos em outra instalação/caminho absoluto.
    legado = ANEXOS / Path(valor).name
    return legado if legado.is_file() else None


async def arquivar(guild: discord.Guild) -> int:
    """Acrescenta ao arquivo as mensagens novas de cada canal. Ignora as do próprio bot. Devolve quantas gravou."""
    PASTA.mkdir(parents=True, exist_ok=True)
    cursores = _ler_json(CURSORES, {})
    indice = _ler_json(INDICE_AVATARES, {})
    proprio = context.bot().user.id
    gravadas = 0
    for canal_id in _canais_do_arquivo(guild):
        chave = str(canal_id)
        cursor = cursores.get(chave)
        ultimo_arquivado = _ultimo_id_arquivado(canal_id)
        if ultimo_arquivado and (cursor is None or int(ultimo_arquivado) > int(cursor)):
            cursor = ultimo_arquivado
        try:
            lote = await _buscar(canal_id, cursor)
        except (rest.DiscordHTTPError, rest.DiscordTransportError, RuntimeError) as exc:
            log.warning("canal %s não lido: %s", canal_id, exc)
            continue
        if not lote:
            continue
        lote.sort(key=lambda m: int(m["id"]))
        linhas: list[dict] = []
        ultimo_processado = cursor
        for m in lote:
            # Só avança o cursor depois de preservar o conteúdo e todos os anexos.
            # Se o download falhar, a próxima execução volta a buscar essa mensagem.
            registro = _registro(m, canal_id, indice)
            falhou = False
            for anexo in registro["anexos"]:
                anexo["arquivo"] = await _guardar_anexo(anexo, guild.filesize_limit)
                if not anexo["arquivo"]:
                    falhou = True
                    log.error(
                        "mensagem %s do canal %s não arquivada: anexo %s não foi baixado",
                        registro["id"], canal_id, anexo.get("nome")
                    )
                    break
            if falhou:
                break
            autor = (m.get("author") or {}).get("id")
            if autor != str(proprio):
                linhas.append(registro)
            ultimo_processado = str(m["id"])
        if linhas:
            with (PASTA / f"{chave}.jsonl").open("a", encoding="utf-8") as arq:
                for r in linhas:
                    arq.write(json.dumps(r, ensure_ascii=False) + "\n")
            gravadas += len(linhas)
        if ultimo_processado is not None:
            cursores[chave] = ultimo_processado
    atomic_text(CURSORES, json.dumps(cursores))
    log.info("arquivo de mensagens: %d mensagens gravadas", gravadas)
    return gravadas


# ── Restauração ───────────────────────────────────────────────────────────────


class Pausado(Exception):
    """O dono pediu para parar. O checkpoint já está gravado; continuar retoma a partir dele."""


_CAMPOS_EMBED = ("title", "type", "description", "url", "color", "fields", "timestamp", "footer", "author")
_WEBHOOKS: dict[int, tuple[str, str]] = {}


def _ler_canal(canal_id: str) -> list[dict]:
    caminho = PASTA / f"{canal_id}.jsonl"
    if not caminho.exists():
        return []
    saida = []
    for linha in caminho.read_text(encoding="utf-8").splitlines():
        try:
            saida.append(json.loads(linha))
        except ValueError:
            continue
    return saida


def _embeds_limpos(embeds: list[dict]) -> list[dict]:
    """Reenvia o embed como estava. Miniatura e imagem voltam só com a URL (não o objeto da API)."""
    saida = []
    for e in embeds or []:
        limpo = {k: e[k] for k in _CAMPOS_EMBED if k in e}
        for chave in ("image", "thumbnail", "video"):
            if isinstance(e.get(chave), dict) and e[chave].get("url"):
                limpo[chave] = {"url": e[chave]["url"]}
        saida.append(limpo)
    return saida[:10]


def _conteudo(r: dict, limite: int, agora: float) -> tuple[str, list[tuple[str, bytes, str]], list[str]]:
    """Texto da mensagem, arquivos a subir e avisos. Nunca descarta a mensagem por causa de anexo.

    Imagem, GIF e vídeo vão como link (preview). Outros arquivos sobem como arquivo, usando o que foi
    guardado no backup. Se nada disso existir, o link ainda válido é usado; senão fica um aviso.
    """
    textos: list[str] = []
    arquivos: list[tuple[str, bytes, str]] = []
    avisos: list[str] = []
    for a in r.get("anexos") or []:
        nome = a.get("nome") or "arquivo"
        url = a.get("url")
        tipo = a.get("tipo") or "application/octet-stream"
        local = _arquivo_anexo(a.get("arquivo"))
        cabe = (a.get("tamanho") or 0) <= limite and len(arquivos) < regras.MAX_ARQUIVOS_POR_MENSAGEM
        tem_arquivo = local is not None and cabe
        if regras.e_midia(a.get("tipo"), nome):
            if regras.url_valida(url, agora):
                textos.append(url)
            elif tem_arquivo:
                arquivos.append((nome, local.read_bytes(), tipo))
            else:
                avisos.append(f"[mídia indisponível: {nome}]")
            continue
        if tem_arquivo:
            arquivos.append((nome, local.read_bytes(), tipo))
        elif regras.url_valida(url, agora):
            textos.append(url)
        else:
            avisos.append(f"[anexo indisponível: {nome}]")
    texto = regras.limpar_links(r.get("conteudo") or "")
    partes = [p for p in [texto, *textos, *avisos] if p]
    return "\n".join(partes), arquivos, avisos


def _payload(r: dict, *, webhook: bool, limite: int, agora: float) -> tuple[dict, list, bool, list[str]]:
    """Monta o envio de uma mensagem arquivada. Devolve (payload, arquivos, usa Components V2, avisos)."""
    texto, arquivos, avisos = _conteudo(r, limite, agora)
    payload: dict = {"allowed_mentions": {"parse": []}}
    if webhook:
        autor = r["autor"]
        base = autor.get("apelido") or autor.get("nome_publico") or autor.get("usuario")
        payload["username"] = regras.nome_mensagem(base, regras.hora_de_belem(r.get("data")))
        payload["avatar_url"] = autor.get("foto_url") or None
    if r.get("componentes"):
        comps = list(r["componentes"])
        if texto:
            comps.append({"type": 10, "content": texto[:2000]})
        payload["components"] = comps or [{"type": 10, "content": "-"}]
        payload["flags"] = FLAG_COMPONENTES_V2
        return payload, arquivos, True, avisos
    payload["content"] = (texto or "-")[:2000]
    payload["embeds"] = _embeds_limpos(r.get("embeds") or [])
    return payload, arquivos, False, avisos


async def _webhook(canal: discord.TextChannel, recriar: bool = False) -> tuple[str, str]:
    if not recriar and canal.id in _WEBHOOKS:
        return _WEBHOOKS[canal.id]
    achado = None
    if not recriar:
        for wh in await canal.webhooks():
            if (
                wh.name == NOME_WEBHOOK
                and wh.token
                and wh.user
                and wh.user.id == context.bot().user.id
            ):
                achado = wh
                break
    if achado is None:
        achado = await canal.create_webhook(name=NOME_WEBHOOK, reason="Restaurar mensagens")
    from . import incidente

    incidente._replay.add(achado.id)
    _WEBHOOKS[canal.id] = (str(achado.id), achado.token)
    return _WEBHOOKS[canal.id]


async def _enviar_webhook(pai, destino, payload, arquivos, v2: bool) -> dict:
    """Envia pelo webhook. Se o webhook sumiu (404), recria e tenta uma vez. Nunca repete após timeout."""
    thread = f"thread_id={destino.id}" if isinstance(destino, discord.Thread) else ""
    for tentativa in range(2):
        wid, token = await _webhook(pai, recriar=tentativa > 0)
        partes = [thread, "wait=true"] + (["with_components=true"] if v2 else [])
        caminho = f"/webhooks/{wid}/{token}?" + "&".join(p for p in partes if p)
        corpo = payload
        if arquivos:
            corpo = {**payload, "attachments": [{"id": i, "filename": n} for i, (n, _, _) in enumerate(arquivos)]}
        try:
            return await rest.request("POST", caminho, corpo, arquivos=arquivos or None)
        except rest.DiscordHTTPError as exc:
            if exc.status == 404 and tentativa == 0:
                _WEBHOOKS.pop(pai.id, None)
                continue
            raise
    raise RuntimeError("webhook indisponível")


async def _enviar_proprio(destino, payload, arquivos) -> dict:
    corpo = payload
    if arquivos:
        corpo = {**payload, "attachments": [{"id": i, "filename": n} for i, (n, _, _) in enumerate(arquivos)]}
    return await rest.request("POST", f"/channels/{destino.id}/messages", corpo, arquivos=arquivos or None)


def _assinatura_atual(m: dict):
    return reconciliar.assinatura_mensagem({
        "conteudo": m.get("content") or "",
        "embeds": _embeds_limpos(m.get("embeds") or []),
        "anexos": [{"nome": a.get("filename"), "tamanho": a.get("size")}
                   for a in m.get("attachments") or []],
    })


def _autor_bate(registro: dict, atual: dict) -> bool:
    autor = registro.get("autor") or {}
    visto = atual.get("author") or {}
    if str(visto.get("id")) == str(autor.get("id")):
        return True
    if not atual.get("webhook_id"):
        return False
    base = autor.get("apelido") or autor.get("nome_publico") or autor.get("usuario")
    esperado = regras.nome_mensagem(base, regras.hora_de_belem(registro.get("data")))
    return visto.get("username") == esperado


def _conteudo_bate(registro: dict, atual: dict) -> bool:
    original = reconciliar.assinatura_mensagem({
        **registro, "embeds": _embeds_limpos(registro.get("embeds") or [])})
    if original == _assinatura_atual(atual):
        return True
    # O checkpoint do replay cobre mensagens cujo anexo mudou de representação.
    return False


def _mensagem_controle(mensagem: dict) -> bool:
    """Mensagens de confirmação e vitrines são estado funcional do bot."""
    componentes = mensagem.get("componentes", mensagem.get("components")) or []
    from . import shelves

    ids = shelves._custom_ids_da_mensagem(componentes)
    return any(cid.startswith("rst:") or shelves.vitrine_pelo_custom_id(cid) for cid in ids)


async def limpar_extras_snapshot(
    guild: discord.Guild,
    snapshot_dados: dict,
    mapa: dict[str, str],
    *,
    checar=lambda: None,
) -> dict:
    """Apaga mensagens que não pertencem ao snapshot antes de reconstruir o servidor.

    Faz multiconjunto por conteúdo, anexos e autor; não usa IDs de mensagem para
    decidir equivalência. Mensagens equivalentes do próprio bot ficam; as que não
    existem no snapshot são removidas. Só controles de restore/vitrine são
    preservados como mensagens operacionais.
    Exclusões respeitam a janela de 14 dias da API de bulk delete.
    """
    if not snapshot_dados.get("completo"):
        return {"removidas": 0, "falhas": ["snapshot de mensagens incompleto; limpeza de histórico omitida"]}

    sem = asyncio.Semaphore(settings.INCIDENTE_CONCORRENCIA_EXCLUSOES)
    agora = discord.utils.utcnow()

    async def limpar_canal(antigo, registros):
        if mapa.get(f"sem_membros:{antigo}"):
            return 0, []
        destino_id = mapa.get(str(antigo))
        if not destino_id:
            return 0, []  # canal ausente será recriado; ID salvo não é identidade
        try:
            destino = await _destino(guild, destino_id)
            atuais = await _buscar_completo(destino.id)
        except discord.NotFound:
            return 0, []  # recurso ausente será recriado na fase estrutural
        except (discord.HTTPException, rest.DiscordHTTPError, rest.DiscordTransportError, RuntimeError) as exc:
            return 0, [f"canal {antigo}: leitura para limpeza falhou ({type(exc).__name__})"]

        atuais = [m for m in atuais if not _mensagem_controle(m)]
        # Índice por conteúdo e autoria reduz o casamento de n mensagens
        # repetidas de O(n²) para O(n), sem usar IDs como identidade.
        buckets = defaultdict(lambda: defaultdict(deque))
        for indice, mensagem in enumerate(atuais):
            assinatura = _assinatura_atual(mensagem)
            autor_id = str((mensagem.get("author") or {}).get("id"))
            buckets[assinatura][("id", autor_id)].append(indice)
            if mensagem.get("webhook_id"):
                buckets[assinatura][("nome", str((mensagem.get("author") or {}).get("username")))].append(indice)
        usados = set()

        def retirar_indice(fila):
            while fila and fila[0] in usados:
                fila.popleft()
            if not fila:
                return None
            indice = fila.popleft()
            usados.add(indice)
            return indice

        extras = []
        for registro in registros:
            if _mensagem_controle(registro):
                continue
            assinatura = reconciliar.assinatura_mensagem({
                **registro, "embeds": _embeds_limpos(registro.get("embeds") or [])
            })
            por_autor = buckets.get(assinatura, {})
            autor_id = str((registro.get("autor") or {}).get("id"))
            equivalente = retirar_indice(por_autor.get(("id", autor_id), deque()))
            if equivalente is None:
                autor = registro.get("autor") or {}
                base = autor.get("apelido") or autor.get("nome_publico") or autor.get("usuario")
                if base:
                    nome = regras.nome_mensagem(base, regras.hora_de_belem(registro.get("data")))
                    equivalente = retirar_indice(por_autor.get(("nome", str(nome)), deque()))
            if equivalente is None:
                continue

        extras.extend(mensagem for indice, mensagem in enumerate(atuais) if indice not in usados)
        if not extras:
            return 0, []

        ids_novos, ids_antigos = [], []
        limite_antigo = agora - timedelta(days=14)
        for mensagem in extras:
            try:
                criado = discord.utils.snowflake_time(int(mensagem["id"]))
            except (KeyError, TypeError, ValueError):
                ids_antigos.append(str(mensagem.get("id")))
                continue
            (ids_antigos if criado <= limite_antigo else ids_novos).append(str(mensagem["id"]))

        removidas = 0
        falhas = []
        async with sem:
            for inicio in range(0, len(ids_novos), 100):
                checar()
                lote = ids_novos[inicio:inicio + 100]
                if len(lote) == 1:
                    try:
                        await rest.request("DELETE", f"/channels/{destino.id}/messages/{lote[0]}")
                        removidas += 1
                    except rest.DiscordHTTPError as exc:
                        if exc.status != 404 and exc.codigo != 10008:
                            falhas.append(f"remoção {lote[0]}: {exc}")
                    continue
                try:
                    await rest.request("POST", f"/channels/{destino.id}/messages/bulk-delete", {"messages": lote})
                    removidas += len(lote)
                except rest.DiscordHTTPError as exc:
                    if exc.status in (404,) or exc.codigo == 10008:
                        continue
                    # Erros por idade/estado de uma mensagem não interrompem a
                    # limpeza das demais: tenta cada item e registra falhas reais.
                    for mid in lote:
                        try:
                            await rest.request("DELETE", f"/channels/{destino.id}/messages/{mid}")
                            removidas += 1
                        except rest.DiscordHTTPError as individual:
                            if individual.status != 404 and individual.codigo != 10008:
                                falhas.append(f"remoção {mid}: {individual}")
                except rest.DiscordTransportError as exc:
                    falhas.append(f"lote de remoção: {exc}")
            for mid in ids_antigos:
                checar()
                try:
                    await rest.request("DELETE", f"/channels/{destino.id}/messages/{mid}")
                    removidas += 1
                except rest.DiscordHTTPError as exc:
                    if exc.status != 404 and exc.codigo != 10008:
                        falhas.append(f"remoção {mid}: {exc}")
                except rest.DiscordTransportError as exc:
                    falhas.append(f"remoção {mid}: {exc}")
        return removidas, falhas

    total, falhas = 0, []
    canais = list(snapshot_dados.get("canais", {}).items())
    # Limita concorrência de leituras e exclusões; cada chamada respeita o
    # retry_after do cliente REST.
    for inicio in range(0, len(canais), 8):
        resultados = await asyncio.gather(*(
            limpar_canal(antigo, registros) for antigo, registros in canais[inicio:inicio + 8]
        ))
        for removidas, erros in resultados:
            total += removidas
            falhas.extend(erros)
        log.info(
            "pré-limpeza de mensagens: %d/%d canais processados; removidas=%d falhas=%d",
            min(inicio + 8, len(canais)), len(canais), total, len(falhas),
        )
    log.info("pré-limpeza de mensagens: removidas=%d falhas=%d", total, len(falhas))
    return {"removidas": total, "falhas": falhas[:50]}


async def _sincronizar(destino, registros: list[dict], atuais: list[dict], enviar, checar,
                       *, apagar_extras: bool = False) -> tuple[int, list[str]]:
    """Reconcilia por multiconjunto de conteúdo e confirma o envio antes de continuar.

    Mensagens de controle do bot não entram em `atuais`; exclusões são restritas
    à fase explicitamente autorizada e nunca concorrem com reenvios.
    """
    por_id = {str(m.get("id")): j for j, m in enumerate(atuais)}
    por_assinatura_autor = defaultdict(lambda: defaultdict(deque))
    for indice, atual in enumerate(atuais):
        assinatura = _assinatura_atual(atual)
        autor = atual.get("author") or {}
        por_assinatura_autor[assinatura][("id", str(autor.get("id")))].append(indice)
        if atual.get("webhook_id"):
            por_assinatura_autor[assinatura][("nome", str(autor.get("username")))].append(indice)
    usados = set()

    def retirar_indice(fila):
        while fila and fila[0] in usados:
            fila.popleft()
        if not fila:
            return None
        indice = fila.popleft()
        usados.add(indice)
        return indice

    faltam = []
    for i, registro in enumerate(registros):
        replay = store.get_config(f"replay:{destino.id}:{registro['id']}")
        j = por_id.get(str(replay)) if replay else None
        # O checkpoint identifica onde o replay foi enviado, mas não prova que
        # a mensagem continua intacta. Ela pode ter sido editada depois.
        if (
            j is not None
            and j not in usados
            and _autor_bate(registro, atuais[j])
            and _conteudo_bate(registro, atuais[j])
        ):
            usados.add(j)
            continue
        assinatura = reconciliar.assinatura_mensagem({
            **registro,
            "embeds": _embeds_limpos(registro.get("embeds") or []),
        })
        por_autor = por_assinatura_autor.get(assinatura, {})
        autor = registro.get("autor") or {}
        j = retirar_indice(por_autor.get(("id", str(autor.get("id"))), deque()))
        if j is None:
            base = autor.get("apelido") or autor.get("nome_publico") or autor.get("usuario")
            if base:
                esperado = regras.nome_mensagem(base, regras.hora_de_belem(registro.get("data")))
                j = retirar_indice(por_autor.get(("nome", str(esperado)), deque()))
        if j is None:
            faltam.append(i)
    sobram = set(range(len(atuais))) - usados
    erros: list[str] = []
    if apagar_extras:
        for j in sorted(sobram):
            checar()
            m = atuais[j]
            if not m.get("webhook_id"):
                continue  # preserva comandos, confirmações e outras mensagens próprias
            try:
                await rest.request("DELETE", f"/channels/{destino.id}/messages/{m['id']}")
            except rest.DiscordHTTPError as exc:
                if exc.status != 404 and exc.codigo != 10008:
                    erros.append(f"remoção {m['id']}: {exc}")
            except rest.DiscordTransportError as exc:
                erros.append(f"remoção {m['id']}: {exc}")
    enviadas = 0
    for i in faltam:
        checar()
        r = registros[i]
        try:
            resposta = await enviar(r)
        except Pausado:
            raise
        except (rest.DiscordHTTPError, rest.DiscordTransportError) as exc:
            erros.append(f"reenvio {r.get('id', i)}: {exc}")
            continue
        except Exception as exc:
            log.exception("mensagem do índice %s não restaurada; seguindo com as próximas", i)
            erros.append(f"reenvio {r.get('id', i)}: {type(exc).__name__}")
            continue
        if not resposta.get("id"):
            raise RuntimeError("Discord não confirmou o ID da mensagem restaurada")
        store.set_config(f"replay:{destino.id}:{r['id']}", str(resposta["id"]))
        enviadas += 1
    return enviadas, erros


async def _renovar_links(registros: list[dict]) -> int:
    """Pede à API links novos para anexos com link expirado e sem arquivo guardado.

    A Discord só devolve link novo para arquivo que ainda existe nos servidores dela. Se o arquivo
    já foi removido, o link continua expirado e a mensagem sai com aviso.
    """
    import time as _tempo

    agora = _tempo.time()
    pendentes = [
        a
        for r in registros
        for a in r.get("anexos") or []
        if a.get("url")
        and not regras.url_valida(a["url"], agora)
        and _arquivo_anexo(a.get("arquivo")) is None
    ]
    urls = sorted({a["url"] for a in pendentes})
    if not urls:
        return 0
    trocadas: dict[str, str] = {}
    for i in range(0, len(urls), 50):
        try:
            resposta = await rest.request(
                "POST", "/attachments/refresh-urls", {"attachment_urls": urls[i : i + 50]}
            )
        except (rest.DiscordHTTPError, rest.DiscordTransportError) as exc:
            log.warning("não consegui renovar links de anexos: %s", exc)
            continue
        for item in (resposta.get("refreshed_urls") if isinstance(resposta, dict) else None) or []:
            if item.get("original") and item.get("refreshed"):
                trocadas[item["original"]] = item["refreshed"]
    for a in pendentes:
        if a["url"] in trocadas:
            a["url"] = trocadas[a["url"]]
    return len(trocadas)


async def _destino(guild: discord.Guild, novo: str):
    destino = guild.get_channel(int(novo)) or guild.get_thread(int(novo))
    if destino is None:
        destino = await guild.fetch_channel(int(novo))
    return destino


async def restaurar_snapshot(
    guild: discord.Guild,
    snapshot_dados: dict,
    mapa: dict[str, str],
    *,
    fase: str,
    bot_id: int,
    checar=lambda: None,
) -> dict:
    """A fase `proprias` preserva o histórico comum até autorização explícita.
    Na fase `membros`, mensagens do próprio bot são enviadas pelo bot; outros autores usam webhook.

    Só mexe nas mensagens da fase (a outra fase fica intacta). Nunca apaga o que não está no backup
    do mesmo tipo. Falhas são devolvidas e impedem a conclusão; nada é descartado em silêncio.
    """
    canais_snapshot = snapshot_dados.get("canais") or {}
    if not isinstance(canais_snapshot, dict) or not canais_snapshot:
        return {"enviadas": 0, "falhas": ["backup sem mensagens salvas para reproduzir"]}
    limite = getattr(guild, "filesize_limit", None) or regras.LIMITE_ARQUIVO_PADRAO
    import time as _tempo

    total = 0
    falhas: list[str] = []
    if fase == "proprias":
        # Vitrines têm identidade funcional e são restauradas em shelves.conferir.
        # O restante do histórico do bot requer a mesma autorização que o histórico comum.
        return {"enviadas": 0, "falhas": []}
    if settings.GOOGLE_DRIVE_ENABLED:
        ausentes = sorted({
            str(anexo["arquivo"])
            for registros in canais_snapshot.values()
            if isinstance(registros, list)
            for registro in registros
            if not _mensagem_controle(registro)
            for anexo in registro.get("anexos") or []
            if anexo.get("arquivo") and _arquivo_anexo(anexo.get("arquivo")) is None
        })
        if ausentes:
            try:
                from . import google_drive

                recuperados = await google_drive.baixar_arquivos_backup(ausentes)
                falhas.extend(
                    f"mídia {rel} não encontrada na cópia do Google Drive"
                    for rel, path in recuperados.items() if path is None
                )
            except Exception as exc:
                log.exception("não foi possível recuperar mídias ausentes do Google Drive")
                falhas.append(f"download de mídias do Google Drive falhou ({type(exc).__name__})")
    for antigo, registros in canais_snapshot.items():
        if not isinstance(registros, list):
            falhas.append(f"canal {antigo}: registros de mensagens inválidos no backup")
            continue
        if mapa.get(f"sem_membros:{antigo}"):
            continue  # thread sem membros: não foi recriada de propósito
        # Os componentes de vitrine conservam sua identidade mesmo quando o ID muda.
        from . import shelves
        def controle(m):
            componentes = m.get("componentes", m.get("components")) or []
            ids = shelves._custom_ids_da_mensagem(componentes)
            return any(cid.startswith("rst:") or shelves.vitrine_pelo_custom_id(cid)
                       for cid in ids)
        validos = [
            r for r in registros
            if isinstance(r, dict) and r.get("id") is not None
            and isinstance(r.get("autor"), dict)
        ]
        if len(validos) != len(registros):
            falhas.append(f"canal {antigo}: mensagens inválidas foram ignoradas")
        filtrados = [r for r in validos if not controle(r)]
        if not filtrados:
            continue
        novo = mapa.get(str(antigo))
        if not novo:
            falhas.append(f"canal {antigo} não foi remapeado para a restauração de mensagens")
            continue
        try:
            destino = await _destino(guild, novo)
        except discord.HTTPException as exc:
            falhas.append(f"canal {antigo} não encontrado: {exc}")
            continue
        arquivada = isinstance(destino, discord.Thread) and destino.archived
        try:
            if arquivada:
                await destino.edit(archived=False, reason="Restaurar mensagens")
            await _renovar_links(filtrados)
            atuais_todas = await _buscar_completo(destino.id)
            atuais = [m for m in atuais_todas if not controle(m)]

            async def enviar(r, _destino=destino):
                agora = _tempo.time()
                if str(r["autor"].get("id")) == str(bot_id):
                    payload, arquivos, v2, avisos = _payload(r, webhook=False, limite=limite, agora=agora)
                    resposta = await _enviar_proprio(_destino, payload, arquivos)
                else:
                    pai = _destino.parent if isinstance(_destino, discord.Thread) else _destino
                    payload, arquivos, v2, avisos = _payload(r, webhook=True, limite=limite, agora=agora)
                    resposta = await _enviar_webhook(pai, _destino, payload, arquivos, v2)
                for aviso in avisos:
                    log.warning("mensagem %s do canal %s: %s", r["id"], antigo, aviso)
                return resposta

            enviadas, erros = await _sincronizar(
                destino, filtrados, atuais, enviar, checar,
                # Snapshot incompleto permite replay best-effort do conteúdo
                # presente, mas nunca prova que mensagens atuais sejam extras.
                apagar_extras=bool(snapshot_dados.get("completo")),
            )
            total += enviadas
            falhas.extend(f"{antigo}: {e}" for e in erros)
            log.info(
                "replay de mensagens: canal do snapshot %s concluído; enviadas=%d falhas=%d",
                antigo, enviadas, len(erros),
            )
        except Pausado:
            raise
        except (rest.DiscordHTTPError, rest.DiscordTransportError, discord.HTTPException, RuntimeError) as exc:
            falhas.append(f"canal {antigo}: {exc}")
        finally:
            if arquivada:
                try:
                    await destino.edit(archived=True, reason="Estado do backup")
                except discord.HTTPException as exc:
                    falhas.append(f"arquivamento {antigo}: {exc}")
    return {"enviadas": total, "falhas": falhas[:50]}


async def _buscar_completo(canal_id: int) -> list[dict]:
    """Todas as mensagens do canal. Sem limite de quantidade."""
    saida: list[dict] = []
    antes = None
    while True:
        qs = "?limit=100" + (f"&before={antes}" if antes else "")
        lote = await rest.request("GET", f"/channels/{canal_id}/messages{qs}") or []
        if not isinstance(lote, list) or not lote:
            break
        saida.extend(lote)
        proximo = min(int(m["id"]) for m in lote)
        if antes is not None and proximo >= int(antes):
            raise RuntimeError(
                f"paginação completa do canal {canal_id} não avançou"
            )
        antes = proximo
        if len(lote) < 100:
            break
    return saida


async def restaurar(guild: discord.Guild, mapa: dict[str, str]) -> int:
    """Compatibilidade com backups antigos (sem snapshot): reenvia o histórico do JSONL nos canais recriados."""
    total = 0
    for antigo, novo in mapa.items():
        if antigo == novo or not _ler_canal(antigo):
            continue
        destino = guild.get_channel(int(novo)) or guild.get_thread(int(novo))
        if destino is None:
            continue
        regs = _ler_canal(antigo)
        for r in regs:
            pai = destino.parent if isinstance(destino, discord.Thread) else destino
            payload, arquivos, v2, _ = _payload(r, webhook=True, limite=regras.LIMITE_ARQUIVO_PADRAO, agora=0)
            try:
                await _enviar_webhook(pai, destino, payload, arquivos, v2)
                total += 1
            except (rest.DiscordHTTPError, rest.DiscordTransportError, discord.HTTPException) as exc:
                log.warning("histórico do canal %s não restaurado: %s", antigo, exc)
    return total
