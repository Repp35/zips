"""Textos de ajuda: o /help (lista de comandos) e o help de Produto e de Estoque (botão Help nessas seções).

O conteúdo vem dos JSON nesta pasta: help.json (campo "mensagem") e help_produto.json / help_estoque.json (campo "texto").
Os dois têm "url": a imagem grande que vai no fim da ajuda (vazia = sem imagem). Emojis entram como {E.nome}
e são resolvidos no envio. Se o texto passar do limite de uma mensagem de componentes, ele vira várias mensagens.
"""

import json
from pathlib import Path

import discord

from ..ui import layout
from ..catalogo import render
from ..data import store
from ..ui.emojis import E
from ..services import notify, rest
from ..config.settings import CORES

PASTA = Path(__file__).resolve().parent
LIMITE_BLOCO = 3500  # um Text Display (o Discord aceita 4000)
LIMITE_TEXTO_MSG = (
    4500  # soma dos textos numa mensagem (o Discord aceita 6000 no total)
)
LIMITE_FILHOS = 30  # blocos de texto por container; com a imagem e o separador, a mensagem fica abaixo de 40


def carregar(nome: str) -> dict:
    dados = json.loads((PASTA / nome).read_text(encoding="utf-8"))
    url = dados.get("url") or ""
    if url and not url.startswith(("https://", "http://")):
        raise ValueError(f"{nome}: url precisa começar com http:// ou https://")
    return dados


def _blocos(texto_md: str) -> list[str]:
    """Quebra o texto em blocos por parágrafo (linha em branco), sem passar de LIMITE_BLOCO."""
    blocos: list[str] = []
    atual = ""
    for paragrafo in texto_md.strip().split("\n\n"):
        candidato = f"{atual}\n\n{paragrafo}" if atual else paragrafo
        if atual and len(candidato) > LIMITE_BLOCO:
            blocos.append(atual)
            atual = paragrafo
        else:
            atual = candidato
    if atual:
        blocos.append(atual)
    return blocos


def mensagens(texto_md: str, url: str, *, efemera: bool) -> list[dict]:
    """Payloads de Components V2 (um por mensagem). A imagem vai só na última mensagem, embaixo."""
    textos = [
        part
        for bloco in _blocos(texto_md)
        for value in [render.texto(bloco)]
        for part in [
            value[i : i + LIMITE_BLOCO] for i in range(0, len(value), LIMITE_BLOCO)
        ]
    ]
    grupos: list[list[str]] = [[]]
    total = 0
    for bloco in textos:
        if grupos[-1] and (
            total + len(bloco) > LIMITE_TEXTO_MSG or len(grupos[-1]) >= LIMITE_FILHOS
        ):
            grupos.append([])
            total = 0
        grupos[-1].append(bloco)
        total += len(bloco)
    saida = []
    for indice, grupo in enumerate(grupos):
        filhos = [layout.texto(bloco) for bloco in grupo]
        if url and indice == len(grupos) - 1:
            filhos += [layout.separador(), layout.imagem(url)]
        saida.append(
            layout.mensagem(
                layout.cartao(*filhos, cor=CORES["neutro"]), efemera=efemera
            )
        )
    return saida


def imagem_de(secao: str, padrao: str) -> str:
    """Imagem do help da seção: a que o dono definiu pelo painel ou pelo /help; senão a do JSON."""
    valor = store.get_config(f"ajuda_imagem:{secao}")
    return padrao if valor is None else valor


def definir_imagem(secao: str, url: str) -> None:
    """Guarda a imagem do help de uma seção. Vazio tira a imagem. Levanta ValueError se o link não servir."""
    url = (url or "").strip()
    if url and (not url.startswith("https://") or " " in url or len(url) > 512):
        raise ValueError("a imagem precisa ser um link https:// sem espaços")
    store.set_config(f"ajuda_imagem:{secao}", url)


def mensagens_help(*, efemera: bool) -> list[dict]:
    dados = carregar("help.json")
    return mensagens(
        dados["mensagem"], imagem_de("geral", dados.get("url", "")), efemera=efemera
    )


async def enviar_interacao(
    interaction: discord.Interaction, payloads: list[dict]
) -> None:
    """Primeira mensagem como resposta da interação; as demais pelo webhook dela (mesmo efêmero ou público)."""
    await rest.responder(interaction, payloads[0])
    for extra in payloads[1:]:
        await rest.request(
            "POST", f"/webhooks/{interaction.application_id}/{interaction.token}", extra
        )


async def enviar_canal(channel_id: int, payloads: list[dict]) -> None:
    for payload in payloads:
        await rest.send_message(channel_id, payload)


async def enviar_help(
    interaction: discord.Interaction,
    arquivo: str,
    secao: str,
) -> None:
    """Help de uma seção: o texto e a imagem, sem botão de volta. Sempre efêmero."""
    dados = carregar(arquivo)
    url = imagem_de(secao, dados.get("url", ""))
    await enviar_interacao(interaction, mensagens(dados["texto"], url, efemera=True))


async def enviar_help_produto(interaction: discord.Interaction) -> None:
    """Botão Help da seção Produto (custom_id ger:produto:help)."""
    await enviar_help(interaction, "help_produto.json", "produto")


async def enviar_help_estoque(interaction: discord.Interaction) -> None:
    """Botão Help da seção Estoque (custom_id ger:estoque:help)."""
    await enviar_help(interaction, "help_estoque.json", "estoque")


class ImagemAjudaModal(discord.ui.Modal):
    """Troca a imagem do help de uma seção ("produto", "estoque" ou "geral"). Só o dono."""

    def __init__(self, secao: str) -> None:
        super().__init__(title=f"Imagem do help: {secao}"[:45])
        self.secao = secao
        atual = imagem_de(secao, "") or None
        self.url = discord.ui.TextInput(
            label="Link https:// (vazio = sem imagem)",
            required=False,
            max_length=512,
            default=atual,
        )
        self.add_item(self.url)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        from ..bot.security import authorize

        if not await authorize(interaction, "owner"):
            return
        try:
            definir_imagem(self.secao, self.url.value)
        except ValueError as exc:
            await interaction.response.send_message(f"{E.wrong} {exc}.", ephemeral=True)
            return
        await interaction.response.send_message(
            f"{E.correct} Imagem do help salva.", ephemeral=True
        )
        await notify.registrar(
            "ajuda_imagem_alterada",
            f"{E.information} Imagem do help alterada",
            CORES["info"],
            [
                ("Help", self.secao),
                ("Imagem", self.url.value.strip() or "removida"),
                ("Por", f"{interaction.user} ({interaction.user.id})"),
            ],
        )
