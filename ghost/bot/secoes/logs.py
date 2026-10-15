"""[ dono ] Logs: canal de logs, envio ligado ou desligado, e eventos ligados ou desligados por categoria.

Tudo em componente, sem JSON. Os eventos vêm de ghost/eventos.py (definições em eventos.json); o estado de cada um
fica no banco e sobrepõe o padrão. Evento desligado não vai ao canal, mas continua indo ao diário.
"""

import discord

from ...core import eventos
from ...ui import layout
from ...catalogo import render
from ...data import store
from ...ui.emojis import E
from ...services import notify, rest
from ...config.settings import CORES
from . import aviso, painel, seletor_entidade, seletor_opcoes, so_dono, sem_acao, voltar

LIMITE_NOME = (
    66  # o rótulo do botão, com "ligado" ou "desligado", cabe em 80 caracteres
)


def _rotulo(evento: "eventos.Evento") -> str:
    nome = (
        evento.nome
        if len(evento.nome) <= LIMITE_NOME
        else evento.nome[: LIMITE_NOME - 1] + "…"
    )
    return f"{nome}: {'ligado' if eventos.ativo(evento.id) else 'desligado'}"


def _envio_ligado() -> bool:
    return store.ajuste("logs_ativo") != "0"


def menu(confirmacao: str | None = None) -> dict:
    canal, origem = store.config_atual("logs_channel_id")
    ligados = sum(1 for e in eventos.EVENTOS.values() if eventos.ativo(e.id))
    itens = []
    if confirmacao:
        itens.append(aviso(confirmacao))
    itens.append(
        layout.texto(
            f"**Canal:** {f'<#{canal}>' if canal else 'não definido'} · {'personalizado' if origem == 'banco' else 'padrão'}\n"
            f"**Envio ao canal:** {'ligado' if _envio_ligado() else 'desligado'}\n"
            f"**Eventos ligados:** {ligados} de {len(eventos.EVENTOS)}"
        )
    )
    itens.append(
        layout.linha(
            render.botao(
                f"Envio: {'ligado' if _envio_ligado() else 'desligado'}",
                "ger:logs:envio",
                3 if _envio_ligado() else 4,
                "@bell",
            )
        )
    )
    itens.append(
        seletor_entidade(
            "canal",
            "ger:logs:canal",
            "Escolha o canal de logs (vazio = padrão)...",
            minimo=0,
            maximo=1,
            padrao=[canal] if canal and canal.isdigit() else None,
        )
    )
    opcoes = [
        {
            "label": nome[:100],
            "value": cid,
            "description": f"{sum(1 for e in eventos.da_categoria(cid) if eventos.ativo(e.id))} ligados"[
                :100
            ],
        }
        for cid, nome in eventos.CATEGORIAS.items()
    ]
    itens.append(
        seletor_opcoes("ger:logs:cat", "Escolha uma categoria de eventos...", opcoes)
    )
    itens.append(voltar())
    return painel(f"{E.bell} Logs", *itens)


def categoria_tela(cid: str, confirmacao: str | None = None) -> dict:
    evs = eventos.da_categoria(cid)
    ligados = sum(1 for e in evs if eventos.ativo(e.id))
    botoes = [
        render.botao(
            _rotulo(e), f"ger:logs:alt:{e.id}:{cid}", 3 if eventos.ativo(e.id) else 4
        )
        for e in evs
    ]
    itens = []
    if confirmacao:
        itens.append(aviso(confirmacao))
    itens.append(
        layout.texto(
            f"**{eventos.CATEGORIAS[cid]}** · {ligados} de {len(evs)} ligados\n"
            "-# Verde = ligado · vermelho = desligado. Clique para alternar."
        )
    )
    itens.extend(layout.linha(*botoes[i : i + 5]) for i in range(0, len(botoes), 5))
    itens.append(
        layout.linha(
            render.botao("Restaurar padrão", f"ger:logs:padrao:{cid}", 1, "@reload"),
            render.botao("Voltar", "ger:logs:menu", 2, "@reload"),
        )
    )
    return painel(f"{E.bell} Logs: {eventos.CATEGORIAS[cid]}", *itens)


async def tratar(interaction: discord.Interaction, acao: str, alvo: list[str]) -> None:
    if not await so_dono(interaction):
        return
    if acao in ("", "menu"):
        return await rest.responder(interaction, menu(), editar=True)
    if acao == "envio":
        ligado = not _envio_ligado()
        store.set_config("logs_ativo", "1" if ligado else "0")
        return await rest.responder(
            interaction,
            menu(confirmacao=f"Envio ao canal {'ligado' if ligado else 'desligado'}."),
            editar=True,
        )
    if acao == "canal":
        valores = interaction.data.get("values") or []
        if not valores:
            store.reset_config("logs_channel_id")
            confirmacao = "Canal voltou ao padrão (.env)."
        elif valores[0].isdigit():
            store.set_config("logs_channel_id", valores[0])
            confirmacao = "Canal de logs salvo."
        else:
            return await sem_acao(interaction)
        return await rest.responder(
            interaction, menu(confirmacao=confirmacao), editar=True
        )
    if acao == "cat":
        valores = interaction.data.get("values") or []
        if not valores or valores[0] not in eventos.CATEGORIAS:
            return await sem_acao(interaction)
        return await rest.responder(
            interaction, categoria_tela(valores[0]), editar=True
        )
    if acao == "alt":
        evento_id = alvo[0] if alvo else ""
        evento = eventos.EVENTOS.get(evento_id)
        if evento is None:
            return await sem_acao(interaction)
        eventos.definir(evento.id, not eventos.ativo(evento.id))
        return await rest.responder(
            interaction, categoria_tela(evento.categoria), editar=True
        )
    if acao == "padrao":
        cid = alvo[0] if alvo else ""
        if cid not in eventos.CATEGORIAS:
            return await sem_acao(interaction)
        eventos.restaurar_padrao(cid)
        await notify.registrar(
            "config_alterada",
            f"{E.bell} Eventos de log restaurados",
            CORES["info"],
            [
                ("Categoria", eventos.CATEGORIAS[cid]),
                ("Por", f"{interaction.user} ({interaction.user.id})"),
            ],
        )
        return await rest.responder(
            interaction,
            categoria_tela(cid, confirmacao="Padrão restaurado."),
            editar=True,
        )
    return await sem_acao(interaction)
