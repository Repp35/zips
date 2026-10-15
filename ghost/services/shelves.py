"""Vitrines: montagem a partir do catálogo, publicação, atualização no lugar e canal de fallback."""

import asyncio
import hashlib
import json
import logging

import discord

from .. import catalogo
from ..ui import layout
from ..config import settings
from ..catalogo import render
from ..data import stock, store
from . import context, rest

log = logging.getLogger("shelves")

SEM_ESTOQUE_MANUAL = "-# Mesmo sem estoque, a entrega pode ser feita manualmente."


def _estado(produto) -> str:
    n = stock.count_global(produto.id)
    if n:
        return f"Estoque: {n}"
    return "Sem estoque" if produto.sem_estoque == "avisar" else "Entrega manual"


def _opcao(produto) -> dict:
    opcao = {
        "label": produto.rotulo,
        "value": produto.id,
        "description": f"{render.brl(store.preco(produto.id))} | {_estado(produto)}",
    }
    icone = render.emoji(produto.emoji)
    if icone:
        opcao["emoji"] = icone
    return opcao


def _linha_estoque(produtos) -> str:
    if len(produtos) == 1:
        n = stock.count_global(produtos[0].id)
        return f"-# Estoque: {n}*" if n else SEM_ESTOQUE_MANUAL
    partes = [f"{p.rotulo}: {n}" for p in produtos if (n := stock.count_global(p.id))]
    return f"-# Estoque: {' | '.join(partes)}*" if partes else SEM_ESTOQUE_MANUAL


def _selecao(v) -> dict:
    d = v.dados
    opcoes = [_opcao(p) for p in catalogo.produtos(vitrine=v.id)]
    return _seletor_cartao(d, opcoes)


def _selecao_simples(v) -> dict:
    d = v.dados
    opcoes = []
    for item in d["opcoes"]:
        opcao = {
            "label": item["label"],
            "value": item["value"],
            "description": item["description"],
        }
        icone = render.emoji(item.get("emoji"))
        if icone:
            opcao["emoji"] = icone
        opcoes.append(opcao)
    return _seletor_cartao(d, opcoes)


def _seletor_cartao(d: dict, opcoes: list[dict]) -> dict:
    select = {
        "type": 3,
        "custom_id": d["custom_id"],
        "placeholder": d["placeholder"],
        "options": opcoes,
    }
    return layout.mensagem(
        layout.cartao(
            layout.imagem(d["imagem"]),
            layout.titulo(d["titulo"]),
            layout.texto(render.texto(d["descricao"])),
            layout.separador(),
            {"type": 1, "components": [select]},
            cor=d.get("cor"),
        )
    )


def _painel(v) -> dict:
    d = v.dados
    produtos = catalogo.produtos(vitrine=v.id)
    ctx = {"estoque": _linha_estoque(produtos)}
    if len(produtos) == 1:
        ctx["preco"] = render.brl(store.preco(produtos[0].id))
    container = render.blocos(d["blocos"], ctx)

    if len(produtos) == 1:
        produto = produtos[0]
        botao = produto.botao or {}
        container.append(
            {
                "type": 1,
                "components": [
                    render.botao(
                        botao.get("label", "Comprar"),
                        f"comprar_{produto.id}",
                        botao.get("style", 2),
                        botao.get("emoji"),
                    )
                ],
            }
        )
    elif produtos:
        seletor = d["seletor"]
        container.append(
            {
                "type": 1,
                "components": [
                    {
                        "type": 3,
                        "custom_id": seletor["custom_id"],
                        "placeholder": seletor["placeholder"],
                        "options": [_opcao(p) for p in produtos],
                    }
                ],
            }
        )
    if d.get("acoes"):
        container.append(
            {
                "type": 1,
                "components": [
                    render.botao(
                        a["label"], a["custom_id"], a.get("style", 2), a.get("emoji")
                    )
                    for a in d["acoes"]
                ],
            }
        )
    return layout.mensagem(layout.cartao(*container))


def build(vitrine_id: str) -> dict:
    v = catalogo.vitrine(vitrine_id)
    if v is None:
        raise KeyError(f"vitrine desconhecida: {vitrine_id}")
    if v.tipo == "selecao":
        return _selecao(v)
    if v.tipo == "selecao_simples":
        return _selecao_simples(v)
    return _painel(v)


def canal_configurado(tipo: str) -> int:
    """Canal que a vitrine deve ter: o campo `canal` do produto que a abre; se ele não define, o canal salvo
    da vitrine (config `<tipo>_channel_id`). 0 quando nenhum dos dois existe."""
    return catalogo.canal_da_vitrine(tipo) or store.channel_for(tipo)


def canal_da_vitrine(tipo: str) -> int | None:
    """Canal configurado; se não houver, o canal da mensagem já publicada."""
    canal = canal_configurado(tipo)
    if canal:
        return canal
    salva = store.setup_message(tipo)
    return int(salva["channel_id"]) if salva else None


async def publish(
    tipo: str, *, fallback_channel: discord.abc.Messageable | None = None
) -> None:
    """Publica a vitrine no canal configurado, substituindo a anterior."""
    antiga = store.setup_message(tipo)

    canal_id = None
    alvo = canal_da_vitrine(tipo)
    if alvo:
        try:
            canal_id = (await context.channel(alvo)).id
        except discord.HTTPException:
            canal_id = None
    if canal_id is None and fallback_channel is not None:
        canal_id = fallback_channel.id
    if canal_id is None:
        raise RuntimeError(f"canal não encontrado para a vitrine {tipo}")

    msg = await rest.send_message(canal_id, build(tipo))
    store.save_setup_message(tipo, int(msg["channel_id"]), int(msg["id"]))
    store.save_vitrine_assinatura(tipo, assinatura(tipo))
    if antiga and int(antiga["message_id"]) != int(msg["id"]):
        await _apagar(int(antiga["channel_id"]), int(antiga["message_id"]))


async def sincronizar(tipo: str) -> str | None:
    """Deixa a vitrine publicada igual ao catálogo. Devolve o motivo quando não dá certo, None quando dá.
    - Vitrine que sumiu (último produto apagado) ou que não é pública: a mensagem é retirada.
    - Sem mensagem publicada, ou com canal diferente do configurado: publica de novo.
    - Nos outros casos: atualiza a mensagem no lugar."""
    v = catalogo.vitrine(tipo)
    if v is None or not v.publicar:
        await retirar(tipo)
        return None
    salva = store.setup_message(tipo)
    alvo = canal_configurado(tipo)
    try:
        if salva is None or (alvo and alvo != int(salva["channel_id"])):
            await publish(tipo)
        else:
            await refresh(tipo)
    except (RuntimeError, discord.HTTPException, KeyError) as exc:
        return str(exc)
    return None


async def retirar(tipo: str) -> None:
    """Apaga a mensagem publicada da vitrine, se houver."""
    salva = store.setup_message(tipo)
    if salva:
        await _apagar(int(salva["channel_id"]), int(salva["message_id"]))
        store.delete_setup_message(tipo)


async def refresh(tipo: str) -> None:
    """Atualiza a vitrine publicada no lugar. Não faz nada se ela não existe."""
    row = store.setup_message(tipo)
    if not row:
        return
    try:
        await rest.edit_message(
            int(row["channel_id"]), int(row["message_id"]), build(tipo)
        )
        store.save_vitrine_assinatura(tipo, assinatura(tipo))
    except RuntimeError:
        log.warning(
            "vitrine %s não pôde ser atualizada; use /setup para republicar", tipo
        )


_pendentes: dict[str, asyncio.Task] = {}
DEBOUNCE_S = 2.0


async def _refresh_adiado(tipo: str) -> None:
    try:
        await asyncio.sleep(DEBOUNCE_S)
        _pendentes.pop(tipo, None)
        await refresh(tipo)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("falha ao atualizar vitrine %s", tipo)
    finally:
        if _pendentes.get(tipo) is asyncio.current_task():
            _pendentes.pop(tipo, None)


async def refresh_for_product(produto_key: str) -> None:
    """Agrupa várias mudanças de estoque seguidas em uma única edição da vitrine."""
    produto = catalogo.produto(produto_key)
    if produto is None or produto.vitrine in _pendentes:
        return
    _pendentes[produto.vitrine] = asyncio.create_task(_refresh_adiado(produto.vitrine))


def vitrine_pelo_custom_id(custom_id: str) -> str | None:
    """Deduz a vitrine a partir de um custom_id antigo (ex.: comprar_capcut_btn → CAPCUT)."""
    tokens = set(custom_id.lower().split("_"))
    for v in catalogo.vitrines():
        if v.id.lower() in tokens:
            return v.id
    return None


def registrar_local(vitrine_id: str, interaction: discord.Interaction) -> None:
    """Guarda onde a vitrine clicada está (canal e mensagem), para que o bot a atualize e
    republique sem exigir /setup de novo."""
    msg = interaction.message
    if msg is None or interaction.channel_id is None:
        return
    salva = store.setup_message(vitrine_id)
    if salva and int(salva["message_id"]) == msg.id:
        return
    store.save_setup_message(vitrine_id, interaction.channel_id, msg.id)
    if not canal_configurado(vitrine_id):
        store.set_config(
            f"{vitrine_id.lower()}_channel_id", str(interaction.channel_id)
        )


async def redesenhar_clicado(interaction: discord.Interaction, vitrine_id: str) -> None:
    """Escolha em seletor: a própria mensagem é editada com o conteúdo novo. O rodapé tem horário,
    então a edição nunca é idêntica à anterior, e o select volta ao placeholder."""
    try:
        await rest.edit_message(
            interaction.channel_id, interaction.message.id, build(vitrine_id)
        )
    except (RuntimeError, AttributeError) as exc:
        log.warning("não foi possível redesenhar a vitrine %s: %s", vitrine_id, exc)


def vitrine_e_seletor(produto_key: str) -> bool:
    """Só vitrines com seletor são atualizadas quando alguém compra; as de botão mudam no boot ou com o conteúdo."""
    produto = catalogo.produto(produto_key)
    if produto is None:
        return False
    v = catalogo.vitrine(produto.vitrine)
    return v is not None and v.tipo in ("selecao", "selecao_simples")


def assinatura(tipo: str) -> str:
    """Hash do conteúdo da vitrine sem o horário do rodapé (que muda a cada edição)."""
    # "layout-2" força a atualização das vitrines que ainda têm o rodapé antigo "Atualizado às".
    texto = json.dumps(build(tipo), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(("layout-2" + texto).encode("utf-8")).hexdigest()


def _custom_ids(tipo: str) -> set[str]:
    v = catalogo.vitrine(tipo)
    if v is None:
        return set()
    if v.tipo in ("selecao", "selecao_simples"):
        return {v.dados["custom_id"]}
    ids = {f"comprar_{p.id}" for p in catalogo.produtos(vitrine=tipo)}
    ids |= {a["custom_id"] for a in v.dados.get("acoes", [])}
    if v.dados.get("seletor"):
        ids.add(v.dados["seletor"]["custom_id"])
    return ids


async def _mensagem_salva(canal_id: int, mensagem_id: int) -> dict | None:
    try:
        return await rest.request("GET", f"/channels/{canal_id}/messages/{mensagem_id}")
    except rest.DiscordHTTPError as exc:
        if exc.status == 404:
            return None
        raise


def _custom_ids_da_mensagem(componentes) -> list[str]:
    """Todos os custom_id de uma mensagem, mesmo dentro de Container, Section e Action Row."""
    achados: list[str] = []
    pilha = list(componentes or [])
    while pilha:
        item = pilha.pop()
        if isinstance(item, dict):
            if isinstance(item.get("custom_id"), str):
                achados.append(item["custom_id"])
            pilha.extend(v for v in item.values() if isinstance(v, (dict, list)))
        elif isinstance(item, list):
            pilha.extend(item)
    return achados


async def _procurar(tipo: str, canal_id: int) -> int | None:
    """Acha a vitrine no canal quando ela não está registrada (ex.: mudou de canal ou o registro se perdeu).
    Reconhece pelo custom_id atual e também pelos antigos (ex.: comprar_capcut_btn), do mesmo jeito
    que o clique na vitrine é reconhecido."""
    dono = str(context.bot().user.id)
    ids = _custom_ids(tipo)
    antes = None
    while True:
        caminho = f"/channels/{canal_id}/messages?limit=100" + (f"&before={antes}" if antes else "")
        try:
            mensagens = await rest.request("GET", caminho)
        except rest.DiscordHTTPError as exc:
            if exc.status == 404:
                return None
            raise
        if not isinstance(mensagens, list) or not mensagens:
            return None
        for m in mensagens:
            if m.get("author", {}).get("id") != dono:
                continue
            for cid in _custom_ids_da_mensagem(m.get("components", [])):
                if cid in ids or vitrine_pelo_custom_id(cid) == tipo:
                    return int(m["id"])
        if len(mensagens) < 100:
            return None
        antes = min(int(m["id"]) for m in mensagens)


async def _achar_no_servidor(tipo: str) -> tuple[int, int] | tuple[None, None]:
    """Sem canal configurado nem registro: procura a vitrine nos canais de texto do servidor
    (pelos custom_id dos botões) e devolve (canal, mensagem) de onde ela já está."""
    if not settings.GUILD_ID:
        log.warning(
            "GUILD_ID não definido no .env: não consigo procurar a vitrine %s no servidor",
            tipo,
        )
        return None, None
    try:
        canais = await rest.request("GET", f"/guilds/{settings.GUILD_ID}/channels")
    except RuntimeError as exc:
        log.warning("não consegui listar os canais do servidor: %s", exc)
        return None, None
    if not isinstance(canais, list):
        return None, None
    for c in canais:
        if c.get("type") not in (0, 5):
            continue
        achada = await _procurar(tipo, int(c["id"]))
        if achada is not None:
            return int(c["id"]), achada
    return None, None


def _tem_v2(flags: int) -> bool:
    return bool(flags & (1 << 15))


async def conferir(tipo: str) -> str:
    """Garante que a vitrine está publicada e igual ao catálogo, sem reenviar o que não mudou.
    Sempre guarda o id atual da mensagem, mesmo quando nada muda."""
    v = catalogo.vitrine(tipo)
    if v is None or not v.publicar:
        await retirar(tipo)
        return "retirada"
    salva = store.setup_message(tipo)
    canal = canal_configurado(tipo) or (int(salva["channel_id"]) if salva else 0)
    if not canal:
        canal, msg_id = await _achar_no_servidor(tipo)
        if canal is None:
            return "sem canal"
        store.save_setup_message(tipo, canal, msg_id)
        store.set_config(f"{tipo.lower()}_channel_id", str(canal))
        salva = store.setup_message(tipo)
        log.info(
            "vitrine %s encontrada no canal %s; canal salvo como padrão", tipo, canal
        )
    if salva and int(salva["channel_id"]) != canal:
        await publish(tipo)
        return "publicada no canal novo"
    achada = None
    if salva and await _mensagem_salva(canal, int(salva["message_id"])) is not None:
        achada = int(salva["message_id"])
    if achada is None:
        achada = await _procurar(tipo, canal)
    if achada is None:
        await publish(tipo)
        return "publicada (não existia)"
    atual = await _mensagem_salva(canal, achada)
    if atual is not None and _tem_v2(atual.get("flags", 0)) != _tem_v2(
        build(tipo).get("flags", 0)
    ):
        # O Discord não troca o tipo de layout de uma mensagem existente: troca a mensagem.
        await _apagar(canal, achada)
        await publish(tipo)
        return "republicada (layout mudou)"
    store.save_setup_message(tipo, canal, achada)
    esperado = build(tipo)
    def resumo_embeds(obj):
        return [(e.get("title"), e.get("description"), e.get("color"))
                for e in obj.get("embeds", [])]
    if (atual is not None and salva and int(salva["message_id"]) == achada
        and store.vitrine_assinatura(tipo) == assinatura(tipo)
        and atual.get("content") == esperado.get("content")
        and resumo_embeds(atual) == resumo_embeds(esperado)
        and set(_custom_ids_da_mensagem(atual.get("components"))) ==
            set(_custom_ids_da_mensagem(esperado.get("components")))):
        return "sem mudança"
    await rest.edit_message(canal, achada, build(tipo))
    store.save_vitrine_assinatura(tipo, assinatura(tipo))
    return "atualizada"


async def republicar_todas() -> list[str]:
    """Confere vitrines e devolve falhas para o restore não anunciar sucesso parcial."""
    falhas = []
    for v in catalogo.vitrines():
        try:
            resultado = await conferir(v.id)
            if resultado == "sem canal":
                falhas.append(f"vitrine {v.id}: canal não encontrado")
                log.warning(
                    "vitrine %s sem canal. Defina %s_CHANNEL_ID no .env, rode /setup add na vitrine, "
                    "ou aponte DB_PATH para o banco antigo que tem a config %s_channel_id.",
                    v.id,
                    v.id.upper(),
                    v.id.lower(),
                )
            else:
                log.info("vitrine %s: %s", v.id, resultado)
        except Exception as exc:
            falhas.append(f"vitrine {v.id}: {type(exc).__name__}")
            log.exception("falha ao conferir vitrine %s", v.id)
    return falhas


async def _apagar(channel_id: int, message_id: int) -> None:
    try:
        msg = await (await context.channel(channel_id)).fetch_message(message_id)
        await msg.delete()
    except discord.HTTPException:
        pass


async def close():
    tasks = tuple(_pendentes.values())
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    _pendentes.clear()
