"""Seções do /gerenciar. Cada módulo expõe `menu()` (payload de Components V2) e
`async tratar(interaction, acao, alvo)`, chamado pelo hub com o que vem depois de `ger:<secao>:`.

Aqui ficam os ajudantes comuns: painéis, paginação, botões on/off, seletores de cargo, usuário e canal,
confirmação e checagem de nível (dono ou admin). Todo botão novo deve ter custom_id único dentro da mensagem.
"""

import discord

from ...ui import layout
from ...config import settings
from ...catalogo import render
from ...data import store
from ...ui.emojis import E
from ...services import rest
from ...config.settings import CORES

TAMANHO_PAGINA = 25  # limite de opções de um select
TIPO_SELETOR = {"cargo": 6, "usuario": 5, "canal": 8}


# ── Painéis ───────────────────────────────────────────────────────────────────


def painel(titulo: str, *itens: dict, cor: str = "neutro") -> dict:
    """Monta a tela. Cada tela põe o próprio Voltar, que leva só ao nível de cima (nunca direto ao Gerenciar)."""
    return layout.mensagem(
        layout.cartao(layout.titulo(titulo), *itens, cor=CORES[cor]), efemera=True
    )


def confirmar(
    titulo: str, texto: str, cid_ok: str, cid_nao: str, rotulo_ok: str = "Confirmar"
) -> dict:
    return painel(
        f"{E.alert} {titulo}",
        layout.texto(texto),
        layout.linha(
            render.botao(rotulo_ok, cid_ok, 4, "@delete"),
            render.botao("Cancelar", cid_nao, 2, "@reload"),
        ),
        cor="aviso",
    )


def voltar(destino: str = "ger:menu", rotulo: str = "Voltar") -> dict:
    return layout.linha(render.botao(rotulo, destino, 2, "@reload"))


def aviso(texto: str) -> dict:
    """Linha de confirmação no topo de um painel."""
    return layout.texto(f"{E.correct} {texto}")


def botao_ligado(ligado: bool, rotulo: str, custom_id: str) -> dict:
    """Botão on/off: verde e 'ligado' quando está ligado, vermelho e 'desligado' quando não está."""
    if ligado:
        return render.botao(f"{rotulo}: ligado", custom_id, 3, "@correct")
    return render.botao(f"{rotulo}: desligado", custom_id, 4, "@wrong")


# ── Paginação ─────────────────────────────────────────────────────────────────


def pagina_de(
    itens: list, pagina: int, tamanho: int = TAMANHO_PAGINA
) -> tuple[list, int, int]:
    """Devolve (itens da página, página corrigida, total de páginas). A página pedida é limitada ao intervalo."""
    total = max(1, -(-len(itens) // tamanho))
    pagina = min(max(int(pagina), 0), total - 1)
    return itens[pagina * tamanho : (pagina + 1) * tamanho], pagina, total


def navegacao(base: str, pagina: int, total: int) -> dict | None:
    """Linha com Anterior e Próxima. `base` é o custom_id sem o número da página."""
    if total <= 1:
        return None
    anterior = render.botao("Anterior", f"{base}:{max(pagina - 1, 0)}", 2, "@reload")
    proxima = render.botao(
        "Próxima", f"{base}:{min(pagina + 1, total - 1)}", 2, "@reload"
    )
    if pagina <= 0:
        anterior["disabled"] = True
    if pagina >= total - 1:
        proxima["disabled"] = True
    return layout.linha(anterior, proxima)


def rodape_pagina(pagina: int, total: int, extra: str = "") -> str:
    texto = f"-# Página {pagina + 1} de {total}"
    return f"{texto} · {extra}" if extra else texto


# ── Seletores ─────────────────────────────────────────────────────────────────


def seletor_opcoes(custom_id: str, placeholder: str, opcoes: list[dict]) -> dict:
    """Select de texto, com a linha pronta. Não monta select vazio: quem chama confere se há opções."""
    return layout.linha(
        layout.seletor(custom_id, placeholder[:150], opcoes[:TAMANHO_PAGINA])
    )


def seletor_entidade(
    tipo: str,
    custom_id: str,
    placeholder: str,
    *,
    minimo: int = 0,
    maximo: int = 25,
    padrao: list[str] | None = None,
) -> dict:
    """Select de cargo, usuário ou canal (tipos 6, 5 e 8). `padrao` mostra o que já está escolhido."""
    componente = {
        "type": TIPO_SELETOR[tipo],
        "custom_id": custom_id,
        "placeholder": placeholder[:150],
        "min_values": minimo,
        "max_values": maximo,
    }
    if tipo == "canal":
        componente["channel_types"] = [0]  # só canais de texto
    if padrao:
        # A API espera o tipo do valor padrão como texto: "user", "role" ou "channel".
        tipo_api = {"cargo": "role", "usuario": "user", "canal": "channel"}[tipo]
        componente["default_values"] = [
            {"id": str(i), "type": tipo_api} for i in padrao[:maximo]
        ]
    return layout.linha(componente)


# ── Permissão ─────────────────────────────────────────────────────────────────


async def so_dono(interaction: discord.Interaction) -> bool:
    if interaction.user.id == settings.ADMIN_ID:
        return True
    await interaction.response.send_message(
        f"{E.wrong} Só o dono pode fazer isso.", ephemeral=True
    )
    return False


async def so_admin(interaction: discord.Interaction) -> bool:
    if store.is_admin(interaction.user.id):
        return True
    await interaction.response.send_message(f"{E.wrong} Sem permissão.", ephemeral=True)
    return False


async def sem_acao(interaction: discord.Interaction) -> None:
    await interaction.response.send_message(
        f"{E.wrong} Ação desconhecida.", ephemeral=True
    )


# ── Envio ─────────────────────────────────────────────────────────────────────


async def seguir(interaction: discord.Interaction, payload: dict) -> None:
    """Mensagem nova e efêmera depois de um defer (a resposta inicial já foi dada). Usa o webhook da interação."""
    await rest.request(
        "POST", f"/webhooks/{interaction.application_id}/{interaction.token}", payload
    )
