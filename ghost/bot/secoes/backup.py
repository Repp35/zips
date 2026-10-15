"""[ dono ] Backup do servidor: ver o último, criar agora e restaurar.

O backup diário e a restauração automática em raid rodam sozinhos (services/backup.py).
Aqui o dono faz isso na mão. As duas ações demoram mais que 3 s, então a resposta é adiada e o painel é atualizado depois.
"""

import discord

from ...ui import layout
from ...catalogo import render
from ...ui.emojis import E
from ...services import backup, rest
from . import aviso, confirmar, painel, so_dono, sem_acao, voltar


def _resumo() -> str:
    dados = backup.ultimo()
    if dados is None:
        return "Nenhum backup ainda. O bot faz um por dia, sozinho."
    return (
        f"**Canais:** {len(dados['canais'])} · **Cargos:** {len(dados['cargos'])}\n"
        "-# Guarda nome, descrição, ícone, cargos e canais com as permissões de cada um."
    )


def menu(confirmacao: str | None = None) -> dict:
    itens = []
    if confirmacao:
        itens.append(aviso(confirmacao))
    itens.append(layout.texto(_resumo()))
    itens.append(
        layout.linha(
            render.botao("Fazer backup agora", "ger:backup:criar", 3, "@save"),
            render.botao("Restaurar último backup", "ger:backup:pedir", 4, "@reload"),
        )
    )
    itens.append(voltar())
    return painel(f"{E.settings} Backup", *itens)


async def _adiar(interaction: discord.Interaction) -> None:
    await rest.request(
        "POST",
        f"/interactions/{interaction.id}/{interaction.token}/callback",
        {"type": 6},
    )


async def _atualizar(interaction: discord.Interaction, payload: dict) -> None:
    await rest.request(
        "PATCH",
        f"/webhooks/{interaction.application_id}/{interaction.token}/messages/@original",
        payload,
    )


async def tratar(interaction: discord.Interaction, acao: str, alvo: list[str]) -> None:
    if not await so_dono(interaction):
        return
    if acao == "menu":
        await rest.responder(interaction, menu(), editar=True)
        return
    if acao == "criar":
        await _adiar(interaction)
        try:
            caminho = (
                await backup.criar(interaction.guild, motivo="manual")
                if interaction.guild
                else None
            )
            texto = (
                f"Backup criado: {caminho.name}"
                if caminho
                else "Não consegui criar o backup."
            )
        except RuntimeError as exc:
            texto = f"Backup cancelado: {exc}"
        await _atualizar(interaction, menu(texto))
        return
    if acao == "pedir":
        if backup.ultimo() is None:
            await rest.responder(
                interaction, menu("Não há backup para restaurar."), editar=True
            )
            return
        await rest.responder(
            interaction,
            confirmar(
                "Restaurar servidor",
                "A restauração compara e corrige estrutura, cargos e permissões conforme o backup. "
                "As vitrines voltam automaticamente. O histórico comum só será refeito após uma segunda "
                "confirmação específica. Continuar?",
                "ger:backup:restaurar",
                "ger:backup:menu",
                rotulo_ok="Restaurar",
            ),
            editar=True,
        )
        return
    if acao == "restaurar":
        await _adiar(interaction)
        if interaction.guild is None:
            texto = "Esta ação só funciona dentro do servidor."
        else:
            resultado = await backup.restaurar(
                interaction.guild,
                motivo=f"manual por {interaction.user} ({interaction.user.id})",
                confirmar=True,
            )
            if "erro" in resultado:
                texto = resultado["erro"]
            elif resultado.get("iniciada"):
                texto = "Restauração iniciada. A confirmação do histórico comum aparecerá no canal de logs."
            else:
                texto = "Restaurado: " + "; ".join(resultado["feitos"])
                if resultado["avisos"]:
                    texto += "\nAvisos: " + "; ".join(resultado["avisos"])
        await _atualizar(interaction, menu(texto[:1500]))
        return
    await sem_acao(interaction)
