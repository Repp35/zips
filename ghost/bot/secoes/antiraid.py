"""[ dono ] Anti-raid: painel com on/off de anti-raid, anti-nuke e anti-spam, e os limites de cada tipo de ação.

Mesmo painel para a seção Anti-raid do /gerenciar, para /antiraid e para >antiraid.
Estado e limites ficam no banco (config: antiraid_ativo, antinuke_ativo, spam_ativo, raid_limites em JSON).
Sem valor no banco, vale o padrão de settings (config.json). Qualquer gravação chama raid.invalidar().
"""

import json
import logging

import discord

from ...ui import layout
from ...config import settings
from ...data import store
from ...ui.emojis import E
from ...services import notify, raid, rest
from ...config.settings import CORES
from . import botao_ligado, painel, seletor_opcoes, so_dono, sem_acao, voltar

log = logging.getLogger("antiraid")

# Tipos de ação do raid, na ordem do painel. Os nomes são os da chave raid.limites do config.json.
ACOES = (
    ("channel_create", "Criação de canais"),
    ("channel_delete", "Exclusão de canais"),
    ("channel_update", "Edição de canais"),
    ("thread_create", "Criação de threads"),
    ("thread_delete", "Exclusão de threads"),
    ("thread_update", "Edição de threads"),
    ("webhook_create", "Criação de webhooks"),
    ("webhook_update", "Edição de webhooks"),
    ("webhook_delete", "Exclusão de webhooks"),
    ("bot_add", "Bots adicionados"),
    ("role_create", "Criação de cargos"),
    ("role_delete", "Exclusão de cargos"),
    ("role_update", "Edição de cargos"),
    ("member_role_update", "Alteração de cargos de membros"),
    ("guild_update", "Alteração das configurações do servidor"),
    ("invite_create", "Criação de convites"),
    ("emoji_create", "Criação de emojis"),
    ("emoji_update", "Edição de emojis"),
    ("emoji_delete", "Exclusão de emojis"),
    ("sticker_create", "Criação de figurinhas"),
    ("sticker_update", "Edição de figurinhas"),
    ("sticker_delete", "Exclusão de figurinhas"),
    ("integration_create", "Criação de integrações"),
    ("integration_update", "Edição de integrações"),
    ("integration_delete", "Exclusão de integrações"),
    ("automod_rule_create", "Criação de regras do AutoMod"),
    ("automod_rule_update", "Edição de regras do AutoMod"),
    ("automod_rule_delete", "Exclusão de regras do AutoMod"),
    ("scheduled_event_create", "Criação de eventos agendados"),
    ("scheduled_event_update", "Edição de eventos agendados"),
    ("scheduled_event_delete", "Exclusão de eventos agendados"),
    ("stage_instance_create", "Criação de sessões Stage"),
    ("stage_instance_update", "Edição de sessões Stage"),
    ("stage_instance_delete", "Exclusão de sessões Stage"),
    ("soundboard_sound_create", "Criação de sons"),
    ("soundboard_sound_update", "Edição de sons"),
    ("soundboard_sound_delete", "Exclusão de sons"),
    ("member_update", "Alterações em membros"),
    ("member_join", "Entradas de membros"),
    ("member_prune", "Remoção em massa de membros"),
    ("message_delete", "Exclusão de mensagens"),
    ("message_bulk_delete", "Exclusão em massa de mensagens"),
    ("ban_kick", "Bans e expulsões"),
    ("unban", "Desbanimentos"),
    ("mencao", "Menções em massa"),
    ("reaction", "Spam de reações"),
)
NOMES = dict(ACOES)
ROTULOS = {
    "antiraid_ativo": "Anti-raid",
    "antinuke_ativo": "Anti-nuke",
    "spam_ativo": "Anti-spam",
}
MAX_ACOES = 1000
MAX_JANELA_S = 3600


# ── Leitura e gravação ────────────────────────────────────────────────────────


def _padrao(tipo: str) -> tuple[int, int]:
    return settings.RAID_LIMITES.get(
        tipo, (settings.SPAM_MAX_MSGS, settings.SPAM_JANELA_S)
    )


def _bruto_banco() -> dict:
    try:
        dados = json.loads(store.get_config("raid_limites") or "{}")
    except ValueError:
        return {}
    return dados if isinstance(dados, dict) else {}


def _gravar_limite(tipo: str, par: tuple[int, int] | None) -> None:
    """par None devolve o tipo ao padrão (apaga a sobreposição do banco)."""
    dados = _bruto_banco()
    if par is None:
        dados.pop(tipo, None)
    else:
        dados[tipo] = [par[0], par[1]]
    if dados:
        store.set_config("raid_limites", json.dumps(dados, ensure_ascii=False))
    else:
        store.reset_config("raid_limites")
    raid.invalidar()


def _valores_modal(dados: dict) -> dict[str, str]:
    """Campos de texto de um modal (vêm dentro de Label, tipo 18). Procura por custom_id em qualquer nível."""
    saida: dict[str, str] = {}

    def andar(no) -> None:
        if isinstance(no, dict):
            if "custom_id" in no and "value" in no:
                saida[no["custom_id"]] = str(no["value"] or "")
            for valor in no.values():
                andar(valor)
        elif isinstance(no, list):
            for item in no:
                andar(item)

    andar((dados or {}).get("components", []))
    return saida


def _inteiro(texto: str, minimo: int, maximo: int) -> int | None:
    t = texto.strip()
    if not t.isdigit():
        return None
    n = int(t)
    return n if minimo <= n <= maximo else None


# ── Painel ────────────────────────────────────────────────────────────────────


def menu(aviso_texto: str | None = None, avulso: bool = False) -> dict:
    """Painel do anti-raid (Components V2, efêmero). `aviso_texto` mostra uma confirmação no topo.
    `avulso` = aberto pelo /antiraid (não dentro do Gerenciar): sem Voltar, e os botões levam o marcador ":s"."""
    sufixo = ":s" if avulso else ""
    limites = raid.limites()
    personalizados = raid.limites_personalizados()
    itens: list[dict] = []
    if aviso_texto:
        itens.append(layout.texto(f"{E.correct} {aviso_texto}"))
    itens.append(
        layout.texto(
            "Cada ação tem limite e janela próprios; ultrapassar o limite inicia contenção e punição. "
            "As reincidências contam por 30 dias. Spam comum: aviso, timeout e ban na terceira ocorrência. "
            "Spam por aplicativo: timeout grave e ban na reincidência. Bots que executam raid são banidos."
        )
    )
    itens.append(layout.linha(*(_botao(chave, sufixo) for chave in ROTULOS)))
    itens.append(
        layout.texto(
            "**Anti-nuke:** restaura canais apagados em massa e faz lockdown.\n"
            "**Anti-spam:** limita mensagens e menções em excesso."
        )
    )
    opcoes = []
    for tipo, nome in ACOES:
        maximo, janela = limites[tipo]
        origem = "personalizado" if tipo in personalizados else "padrão"
        opcoes.append(
            {
                "label": nome,
                "value": tipo,
                "description": f"Até {maximo} em {janela} s ({origem})"[:100],
            }
        )
    metade = (len(opcoes) + 1) // 2
    itens.append(seletor_opcoes(
        f"ger:antiraid:sel{sufixo}",
        "Limites de canais, cargos e convites...",
        opcoes[:metade],
    ))
    itens.append(seletor_opcoes(
        f"ger:antiraid:sel2{sufixo}",
        "Limites de mensagens, integrações e outros...",
        opcoes[metade:],
    ))
    itens.append(
        layout.texto(
            "-# Máximo = ações permitidas; a próxima ação dentro da janela confirma o excesso."
        )
    )
    if not avulso:
        itens.append(voltar())
    return painel(f"{E.shield} Anti-raid", *itens, cor="neutro")


def _botao(chave: str, sufixo: str = "") -> dict:
    return botao_ligado(
        raid.flag(chave), ROTULOS[chave], f"ger:antiraid:toggle:{chave}{sufixo}"
    )


# ── Ações ─────────────────────────────────────────────────────────────────────


async def tratar(interaction: discord.Interaction, acao: str, alvo: list[str]) -> None:
    """Ações do painel. Chamado pelo roteador com o que vem depois de `ger:antiraid:`."""
    if not await so_dono(interaction):
        return
    avulso = "s" in alvo  # marcador dos botões do /antiraid (fora do Gerenciar)
    if acao == "menu":
        await rest.responder(interaction, menu(avulso=avulso), editar=True)
        return
    if acao == "toggle":
        await _alternar(interaction, alvo[0] if alvo else "", avulso)
        return
    if acao in {"sel", "sel2"}:
        await _abrir_formulario(interaction, avulso)
        return
    if acao == "lim":
        await _salvar_limite(interaction, alvo[0] if alvo else "", avulso)
        return
    await sem_acao(interaction)


async def _alternar(
    interaction: discord.Interaction, chave: str, avulso: bool = False
) -> None:
    if chave not in ROTULOS:
        await sem_acao(interaction)
        return
    novo = "0" if raid.flag(chave) else "1"
    store.set_config(chave, novo)
    raid.invalidar()
    estado = "ligado" if novo == "1" else "desligado"
    await notify.registrar(
        "config_alterada",
        f"{E.shield} Anti-raid alterado",
        CORES["info"],
        [
            ("Ajuste", ROTULOS[chave]),
            ("Estado", estado),
            ("Por", f"{interaction.user} ({interaction.user.id})"),
        ],
    )
    await rest.responder(
        interaction, menu(f"{ROTULOS[chave]} {estado}.", avulso), editar=True
    )


async def _abrir_formulario(
    interaction: discord.Interaction, avulso: bool = False
) -> None:
    """Select de ação: abre o modal com o máximo e a janela. Campos vazios valem o padrão."""
    escolhidos = (interaction.data or {}).get("values") or []
    tipo = escolhidos[0] if escolhidos else ""
    if tipo not in NOMES:
        await sem_acao(interaction)
        return
    atual_max, atual_janela = raid.limites()[tipo]
    padrao_max, padrao_janela = _padrao(tipo)
    modal = {
        "type": 9,
        "data": {
            "custom_id": f"ger:antiraid:lim:{tipo}{':s' if avulso else ''}",
            "title": NOMES[tipo][:45],
            "components": [
                {
                    "type": 18,
                    "label": "Máximo de ações",
                    "component": {
                        "type": 4,
                        "custom_id": "maximo",
                        "style": 1,
                        "required": False,
                        "max_length": 4,
                        "placeholder": f"Atual: {atual_max} (vazio = padrão {padrao_max})"[
                            :100
                        ],
                    },
                },
                {
                    "type": 18,
                    "label": "Janela em segundos",
                    "component": {
                        "type": 4,
                        "custom_id": "janela",
                        "style": 1,
                        "required": False,
                        "max_length": 4,
                        "placeholder": f"Atual: {atual_janela} (vazio = padrão {padrao_janela})"[
                            :100
                        ],
                    },
                },
            ],
        },
    }
    await rest.request(
        "POST", f"/interactions/{interaction.id}/{interaction.token}/callback", modal
    )


async def _salvar_limite(
    interaction: discord.Interaction, tipo: str, avulso: bool = False
) -> None:
    if tipo not in NOMES:
        await sem_acao(interaction)
        return
    valores = _valores_modal(interaction.data)
    texto_max = valores.get("maximo", "")
    texto_janela = valores.get("janela", "")
    padrao_max, padrao_janela = _padrao(tipo)
    if not texto_max.strip() and not texto_janela.strip():
        _gravar_limite(tipo, None)
        resumo = f"padrão ({padrao_max} em {padrao_janela} s)"
    else:
        maximo = _inteiro(texto_max, 1, MAX_ACOES) if texto_max.strip() else padrao_max
        janela = (
            _inteiro(texto_janela, 1, MAX_JANELA_S)
            if texto_janela.strip()
            else padrao_janela
        )
        if maximo is None or janela is None:
            await rest.responder(
                interaction,
                {
                    "content": f"{E.wrong} Use número inteiro: máximo de 1 a {MAX_ACOES}, "
                    f"janela de 1 a {MAX_JANELA_S} segundos.",
                    "flags": 64,
                },
            )
            return
        _gravar_limite(
            tipo,
            None
            if (maximo, janela) == (padrao_max, padrao_janela)
            else (maximo, janela),
        )
        resumo = f"{maximo} em {janela} s"
    await notify.registrar(
        "config_alterada",
        f"{E.shield} Limite do anti-raid alterado",
        CORES["info"],
        [
            ("Ajuste", NOMES[tipo]),
            ("Valor", resumo),
            ("Por", f"{interaction.user} ({interaction.user.id})"),
        ],
    )
    await rest.responder(
        interaction, menu(f"Limite de {NOMES[tipo].lower()} salvo: {resumo}.", avulso)
    )
