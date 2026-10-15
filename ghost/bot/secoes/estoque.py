"""[ dono ] Estoque por produto: consultar, adicionar, editar, apagar um item, modelo de JSON, imagem da entrega e limpar tudo.

Contas (login:senha) ficam na tabela estoque; links, em estoque_links. A gravação usa as funções de
ghost/data/stock.py; a leitura do conteúdo, que stock.py não tem, é feita aqui pela conexão de db.py.
Só itens disponíveis (vendido=0) aparecem na consulta. Itens reservados por compra em andamento são contados à parte.
"""

import json
import logging
import math

import discord

from ... import catalogo
from ...ui import layout
from ...catalogo import render
from ...data import stock, store
from ...database.connection import connection
from ...ui.emojis import E
from ...services import avisos, estoque_dm, notify, rest, shelves
from ...config.settings import CORES
from . import (
    aviso,
    confirmar,
    navegacao,
    pagina_de,
    painel,
    rodape_pagina,
    seletor_opcoes,
    so_dono,
    sem_acao,
    voltar,
)
from .produto import _abrir

log = logging.getLogger("secao.estoque")

ITENS_POR_PAGINA = 10
MAX_TEXTO_ITEM = 300  # um link muito longo não pode estourar o campo de texto


# ── Leitura ───────────────────────────────────────────────────────────────────


def _eh_link(pid: str) -> bool:
    return stock.is_link(pid)


def _tabela(pid: str) -> str:
    return "estoque_links" if _eh_link(pid) else "estoque"


def _existe(pid: str) -> bool:
    return catalogo.produto(pid) is not None


def _reservados(pid: str) -> int:
    with connection() as conn:
        return conn.execute(
            f"SELECT COUNT(*) AS c FROM {_tabela(pid)} WHERE produto_key=? AND vendido=1 "
            "AND reservado=1",
            (pid,),
        ).fetchone()["c"]


def _itens(pid: str, offset: int, limite: int) -> list[dict]:
    """Itens disponíveis de uma página, em ordem de entrada."""
    if _eh_link(pid):
        sql = "SELECT id, link AS valor FROM estoque_links WHERE produto_key=? AND vendido=0 AND reservado=0 ORDER BY id LIMIT ? OFFSET ?"
    else:
        sql = (
            "SELECT id, email, senha, extra FROM estoque WHERE produto_key=? AND vendido=0 "
            "ORDER BY id LIMIT ? OFFSET ?"
        )
    with connection() as conn:
        linhas = conn.execute(sql, (pid, limite, offset)).fetchall()
    return [dict(linha) for linha in linhas]


def _contar_disponiveis(pid: str) -> int:
    with connection() as conn:
        return conn.execute(
            f"SELECT COUNT(*) AS c FROM {_tabela(pid)} WHERE produto_key=? AND vendido=0 "
            "AND reservado=0",
            (pid,),
        ).fetchone()["c"]


def _disponiveis(pid: str, offset: int, limite: int) -> list[dict]:
    """Itens disponíveis (id e campos) para escolher na edição ou na exclusão."""
    colunas = "id, link, extra" if _eh_link(pid) else "id, email, senha, extra"
    with connection() as conn:
        linhas = conn.execute(
            f"SELECT {colunas} FROM {_tabela(pid)} WHERE produto_key=? AND vendido=0 "
            "AND reservado=0 ORDER BY id LIMIT ? OFFSET ?",
            (pid, limite, offset),
        ).fetchall()
    return [dict(linha) for linha in linhas]


def _rotulo(pid: str, item: dict) -> str:
    texto = item.get("link", "") if _eh_link(pid) else estoque_dm.resumo(item)
    return f"#{item['id']} · {texto}"[:100]


def _formatar(pid: str, item: dict) -> str:
    if _eh_link(pid):
        texto = item.get("valor") or item.get("link", "")
    else:
        texto = estoque_dm.resumo(item)
    texto = texto.replace("`", "'")
    if len(texto) > MAX_TEXTO_ITEM:
        texto = texto[: MAX_TEXTO_ITEM - 1] + "…"
    return f"`{texto}`"


def _num(alvo: list[str], pos: int = 0) -> int:
    return int(alvo[pos]) if len(alvo) > pos and alvo[pos].isdigit() else 0


# ── Telas ─────────────────────────────────────────────────────────────────────


def _em_estoque(n: int) -> str:
    return (
        "sem estoque" if n == 0 else ("1 em estoque" if n == 1 else f"{n} em estoque")
    )


def menu(confirmacao: str | None = None, pagina: int = 0) -> dict:
    produtos = catalogo.produtos(so_ativos=False)
    itens = []
    if confirmacao:
        itens.append(aviso(confirmacao))
    if not produtos:
        itens.append(layout.texto("Nenhum produto cadastrado."))
        itens.append(voltar())
        return painel(f"{E.cardbox} Estoque", *itens)
    fatia, pagina, total = pagina_de(produtos, pagina)
    opcoes = [
        {
            "label": (p.nome or p.id)[:100],
            "value": p.id,
            "description": f"{_em_estoque(stock.count_global(p.id))} · {p.id}"[:100],
        }
        for p in fatia
    ]
    itens.append(
        layout.texto(
            "Escolha o produto para consultar, adicionar, editar, apagar item ou limpar o estoque.\n"
            + rodape_pagina(pagina, total, f"{len(produtos)} produto(s)")
        )
    )
    itens.append(seletor_opcoes("ger:estoque:pick", "Escolha um produto...", opcoes))
    nav = navegacao("ger:estoque:lista", pagina, total)
    if nav:
        itens.append(nav)
    itens.append(
        layout.linha(
            render.botao("Help", "ger:estoque:help", 2, "@information"),
            render.botao("Imagem do help", "ger:estoque:imagem_ajuda", 2, "@settings"),
        )
    )
    itens.append(voltar())
    return painel(f"{E.cardbox} Estoque", *itens)


def _tipo(pid: str) -> str:
    return "link" if _eh_link(pid) else "conta"


def produto(pid: str, confirmacao: str | None = None) -> dict:
    tipo = "links" if _eh_link(pid) else "contas (login:senha)"
    itens = []
    if confirmacao:
        itens.append(aviso(confirmacao))
    itens.append(
        layout.texto(
            f"**Disponível:** {stock.count_global(pid)} {tipo}\n"
            f"**Reservado em compra:** {_reservados(pid)}\n-# `{pid}`"
        )
    )
    itens.append(
        layout.linha(
            render.botao("Adicionar", f"ger:estoque:add:{pid}", 3, "@plus"),
            render.botao("Editar", f"ger:estoque:editar:{pid}:0", 1, "@save"),
            render.botao("Apagar", f"ger:estoque:apagar:{pid}:0", 4, "@delete"),
            render.botao("Consultar", f"ger:estoque:ver:{pid}:0", 2, "@cardbox"),
            render.botao("Limpar tudo", f"ger:estoque:limpar:{pid}", 4, "@delete"),
        )
    )
    itens.append(
        layout.linha(
            render.botao("Modelo de JSON", f"ger:estoque:modelo:{pid}", 2, "@cardbox"),
            render.botao("Help", "ger:estoque:help", 2, "@information"),
        )
    )
    itens.append(voltar("ger:estoque:menu", "Estoque"))
    return painel(f"{E.cardbox} Estoque: {store.produto_nome(pid)}", *itens)


def modelo_tela(pid: str, confirmacao: str | None = None) -> dict:
    """Escolha do modelo de JSON deste produto, com o JSON padrão e um exemplo."""
    tipo = _tipo(pid)
    chave = estoque_dm.do_produto(pid, tipo)
    opcoes = [
        {"label": m["titulo"], "value": k}
        for k, m in estoque_dm.MODELOS.items()
        if m["tipo"] == tipo
    ]
    itens = []
    if confirmacao:
        itens.append(aviso(confirmacao))
    itens.append(
        layout.texto(
            f"**Modelo deste produto:** {estoque_dm.MODELOS[chave]['titulo']}\n"
            "Escolha outro modelo abaixo se quiser. O modelo define o JSON padrão dos itens."
        )
    )
    itens.append(
        seletor_opcoes(
            f"ger:estoque:escolher_modelo:{pid}", "Escolha o modelo...", opcoes
        )
    )
    itens.append(
        layout.texto(
            f"**JSON padrão:**\n```json\n{estoque_dm.modelo(chave)}\n```\n"
            f"**Exemplo:**\n```json\n{estoque_dm.exemplo(chave)}\n```\n"
            "O Adicional vai por último. `[entrega ticket]` no Adicional faz a entrega manual."
        )
    )
    itens.append(voltar(f"ger:estoque:abrir:{pid}", "Produto"))
    return painel(f"{E.cardbox} Modelo de JSON: {store.produto_nome(pid)}"[:45], *itens)


def consulta(pid: str, pagina: int) -> dict:
    total_itens = stock.count_global(pid)
    total_paginas = max(1, math.ceil(total_itens / ITENS_POR_PAGINA))
    pagina = min(max(pagina, 0), total_paginas - 1)
    linhas = _itens(pid, pagina * ITENS_POR_PAGINA, ITENS_POR_PAGINA)
    corpo = "\n".join(
        f"{n}. {_formatar(pid, i)}"
        for n, i in enumerate(linhas, start=pagina * ITENS_POR_PAGINA + 1)
    )
    itens = [
        layout.texto((corpo or "Nenhum item disponível.")[:3900]),
        layout.rodape(
            rodape_pagina(pagina, total_paginas, f"{_em_estoque(total_itens)}")
        ),
    ]
    nav = navegacao(f"ger:estoque:ver:{pid}", pagina, total_paginas)
    if nav:
        itens.append(nav)
    itens.append(voltar(f"ger:estoque:abrir:{pid}", "Produto"))
    return painel(f"{E.cardbox} Conteúdo: {store.produto_nome(pid)}", *itens)


def limpar_confirmar(pid: str) -> dict:
    return confirmar(
        f"Limpar {store.produto_nome(pid)}",
        f"Isso apaga **todo** o estoque de `{pid}`, inclusive os itens já vendidos guardados aqui. "
        "Itens reservados por entregas em andamento serão preservados. Não dá para desfazer por este painel.",
        f"ger:estoque:limpar_ok:{pid}",
        f"ger:estoque:abrir:{pid}",
        "Limpar de vez",
    )


# ── Modais ────────────────────────────────────────────────────────────────────

# ── Ações ─────────────────────────────────────────────────────────────────────


async def _salvar_json(interaction: discord.Interaction, texto: str, pid: str) -> None:
    """Confere o JSON e adiciona os itens ao estoque do produto."""
    try:
        doc = json.loads(texto)
    except ValueError as exc:
        await interaction.response.send_message(
            f"{E.wrong} JSON inválido: {exc}", ephemeral=True
        )
        return
    tipo = _tipo(pid)
    try:
        itens = estoque_dm.validar(doc, tipo)
    except estoque_dm.EstoqueInvalido as exc:
        await interaction.response.send_message(
            f"{E.wrong} {exc}. Nada foi salvo.", ephemeral=True
        )
        return
    antes = stock.count_global(pid)
    if tipo == "link":
        adicionados = stock.add_links(pid, itens) if itens else 0
    else:
        adicionados = stock.add_contas_detalhadas(pid, itens) if itens else 0
    texto_resposta = (
        f"{E.correct} `{adicionados}` item(ns) adicionado(s) em {store.produto_nome(pid)}."
        if adicionados
        else f"{E.correct} Nenhum item adicionado."
    )
    await interaction.response.send_message(texto_resposta, ephemeral=True)
    await shelves.refresh_for_product(pid)
    if adicionados and antes == 0:
        await avisos.estoque_reposto(pid, adicionados)
    if adicionados:
        await notify.registrar(
            "estoque_adicionado",
            f"{E.cardbox} Estoque adicionado",
            CORES["info"],
            [
                ("Produto", store.produto_nome(pid)),
                ("Itens", adicionados),
                ("Por", f"{interaction.user} ({interaction.user.id})"),
            ],
        )


def _selecao_item(
    pid: str, pagina: int, pick: str, acao: str, titulo: str, texto: str
) -> dict:
    """Escolha de um item disponível (para editar ou apagar), com paginação de 25 por tela."""
    total = _contar_disponiveis(pid)
    total_paginas = max(1, math.ceil(total / 25))
    pagina = min(max(pagina, 0), total_paginas - 1)
    itens = [
        layout.texto(
            texto + "\n" + rodape_pagina(pagina, total_paginas, _em_estoque(total))
        )
    ]
    linhas = _disponiveis(pid, pagina * 25, 25)
    if linhas:
        opcoes = [
            {"label": _rotulo(pid, linha), "value": str(linha["id"])}
            for linha in linhas
        ]
        itens.append(
            seletor_opcoes(f"ger:estoque:{pick}:{pid}", "Escolha o item...", opcoes)
        )
    else:
        itens.append(layout.texto("Nenhum item disponível para escolher."))
    nav = navegacao(f"ger:estoque:{acao}:{pid}", pagina, total_paginas)
    if nav:
        itens.append(nav)
    itens.append(voltar(f"ger:estoque:abrir:{pid}", "Produto"))
    return painel(titulo, *itens)


def _item_do_seletor(interaction: discord.Interaction, pid: str) -> dict | None:
    valores = interaction.data.get("values") or []
    if not valores or not valores[0].isdigit():
        return None
    return stock.item_disponivel(pid, int(valores[0]))


async def _salvar_edicao(
    interaction: discord.Interaction, texto: str, pid: str, item_id: int
) -> None:
    """Confere o JSON de um item só e troca o item disponível pelo novo conteúdo."""
    try:
        doc = json.loads(texto)
    except ValueError as exc:
        await interaction.response.send_message(
            f"{E.wrong} JSON inválido: {exc}", ephemeral=True
        )
        return
    try:
        itens = estoque_dm.validar(doc, _tipo(pid))
    except estoque_dm.EstoqueInvalido as exc:
        await interaction.response.send_message(
            f"{E.wrong} {exc}. Nada foi salvo.", ephemeral=True
        )
        return
    if len(itens) != 1:
        await interaction.response.send_message(
            f"{E.wrong} A edição aceita um item só. Nada foi salvo.", ephemeral=True
        )
        return
    if not stock.substituir_item(pid, item_id, itens[0]):
        await interaction.response.send_message(
            f"{E.wrong} Esse item já foi vendido ou está reservado. Nada foi alterado.",
            ephemeral=True,
        )
        return
    await interaction.response.send_message(
        f"{E.correct} Item `#{item_id}` atualizado.", ephemeral=True
    )
    await shelves.refresh_for_product(pid)
    await notify.registrar(
        "estoque_editado",
        f"{E.cardbox} Item do estoque editado",
        CORES["info"],
        [
            ("Produto", store.produto_nome(pid)),
            ("Item", f"#{item_id}"),
            ("Por", f"{interaction.user} ({interaction.user.id})"),
        ],
    )


async def _apagar_item(
    interaction: discord.Interaction, pid: str, item_id: int
) -> None:
    if not stock.apagar_item(pid, item_id):
        return await rest.responder(
            interaction,
            produto(
                pid,
                confirmacao="Esse item já foi vendido ou está reservado. Nada foi apagado.",
            ),
            editar=True,
        )
    await rest.responder(
        interaction, produto(pid, confirmacao="Item apagado."), editar=True
    )
    await shelves.refresh_for_product(pid)
    await notify.registrar(
        "estoque_removido",
        f"{E.cardbox} Item removido do estoque",
        CORES["aviso"],
        [
            ("Produto", store.produto_nome(pid)),
            ("Item", f"#{item_id}"),
            ("Por", f"{interaction.user} ({interaction.user.id})"),
        ],
    )


# Ações que só o dono executa. O admin vê todos os botões; estas respondem "Só o dono".
ACOES_DONO = {
    "add",
    "editar",
    "pick_editar",
    "apagar",
    "pick_apagar",
    "apagar_ok",
    "ver",
    "escolher_modelo",
    "imagem_ajuda",
    "limpar",
    "limpar_ok",
}


async def tratar(interaction: discord.Interaction, acao: str, alvo: list[str]) -> None:
    if acao in ACOES_DONO and not await so_dono(interaction):
        return
    if acao in ("", "menu"):
        return await rest.responder(interaction, menu(), editar=True)
    if acao == "lista":
        return await rest.responder(interaction, menu(pagina=_num(alvo)), editar=True)
    if acao == "help":
        from ... import ajuda

        return await ajuda.enviar_help_estoque(interaction)
    if acao == "imagem_ajuda":
        from ... import ajuda

        return await interaction.response.send_modal(ajuda.ImagemAjudaModal("estoque"))
    if acao == "pick":
        valores = interaction.data.get("values") or []
        if not valores or not _existe(valores[0]):
            return await sem_acao(interaction)
        return await rest.responder(interaction, produto(valores[0]), editar=True)
    if not alvo or not _existe(alvo[0]):
        return await sem_acao(interaction)
    pid = alvo[0]
    if acao == "abrir":
        return await rest.responder(interaction, produto(pid), editar=True)
    if acao == "ver":
        return await rest.responder(
            interaction, consulta(pid, _num(alvo, 1)), editar=True
        )
    if acao == "add":
        return await _abrir(
            interaction,
            f"Adicionar: {store.produto_nome(pid)}"[:45],
            estoque_dm.modelo(estoque_dm.do_produto(pid, _tipo(pid))),
            lambda i, texto: _salvar_json(i, texto, pid),
        )
    if acao == "editar":
        return await rest.responder(
            interaction,
            _selecao_item(
                pid,
                _num(alvo, 1),
                "pick_editar",
                "editar",
                f"{E.save} Editar item",
                "Escolha o item. O JSON dele abre para você mudar o que quiser.\n",
            ),
            editar=True,
        )
    if acao == "pick_editar":
        item = _item_do_seletor(interaction, pid)
        if item is None:
            return await sem_acao(interaction)
        return await _abrir(
            interaction,
            f"Editar item #{item['id']}",
            json.dumps(
                estoque_dm.json_do_item(item, _tipo(pid)), indent=2, ensure_ascii=False
            ),
            lambda i, texto: _salvar_edicao(i, texto, pid, item["id"]),
        )
    if acao == "apagar":
        return await rest.responder(
            interaction,
            _selecao_item(
                pid,
                _num(alvo, 1),
                "pick_apagar",
                "apagar",
                f"{E.delete} Apagar item",
                "Escolha o item a apagar. Ele some do estoque.\n",
            ),
            editar=True,
        )
    if acao == "pick_apagar":
        item = _item_do_seletor(interaction, pid)
        if item is None:
            return await sem_acao(interaction)
        return await rest.responder(
            interaction,
            confirmar(
                "Apagar item",
                f"Isso apaga este item do estoque de `{pid}`: {_formatar(pid, item)}. Não dá para desfazer.",
                f"ger:estoque:apagar_ok:{pid}:{item['id']}",
                f"ger:estoque:apagar:{pid}:0",
                "Apagar de vez",
            ),
            editar=True,
        )
    if acao == "apagar_ok":
        if len(alvo) < 2 or not alvo[1].isdigit():
            return await sem_acao(interaction)
        return await _apagar_item(interaction, pid, int(alvo[1]))
    if acao == "modelo":
        return await rest.responder(interaction, modelo_tela(pid), editar=True)
    if acao == "escolher_modelo":
        valores = interaction.data.get("values") or []
        if not valores:
            return await sem_acao(interaction)
        try:
            estoque_dm.definir_modelo(pid, valores[0], _tipo(pid))
        except estoque_dm.EstoqueInvalido as exc:
            return await rest.responder(
                interaction, modelo_tela(pid, confirmacao=str(exc)), editar=True
            )
        await rest.responder(
            interaction, modelo_tela(pid, confirmacao="Modelo salvo."), editar=True
        )
        await notify.registrar(
            "estoque_modelo_alterado",
            f"{E.cardbox} Modelo de JSON alterado",
            CORES["info"],
            [
                ("Produto", store.produto_nome(pid)),
                ("Modelo", estoque_dm.MODELOS[valores[0]]["titulo"]),
                ("Por", f"{interaction.user} ({interaction.user.id})"),
            ],
        )
        return None
    if acao == "limpar":
        return await rest.responder(interaction, limpar_confirmar(pid), editar=True)
    if acao == "limpar_ok":
        stock.clear(pid)
        await shelves.refresh_for_product(pid)
        await notify.registrar(
            "estoque_removido",
            f"{E.cardbox} Estoque limpo",
            CORES["aviso"],
            [
                ("Produto", store.produto_nome(pid)),
                ("Por", f"{interaction.user} ({interaction.user.id})"),
            ],
        )
        return await rest.responder(
            interaction, produto(pid, confirmacao="Estoque limpo."), editar=True
        )
    return await sem_acao(interaction)
