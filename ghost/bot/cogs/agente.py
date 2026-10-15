"""Ghost Agent: comandos de controle centralizados em /agente. Só o dono.

Sessão = tarefa por canal (ghost/agente/loop.py). Mensagens do dono no canal vão para a tarefa. Ações de risco
médio e alto pausam com botão de aprovação; a tarefa continua com o resultado.
"""

import logging
import discord
from discord import app_commands
from discord.ext import commands

from ...config import settings
from ...agente import conversa, diario, ferramentas, loop, prompts, provedor
from ...data import store
from ...services import notify
from ...ui.emojis import E
from ..router import route

log = logging.getLogger("agente")

CHAVE_CANAL = "agente_canal"  # canal fixo definido por comando (guardado no banco)
CHAVE_PAUSADO = "agente_pausado"


def _dono(user_id: int) -> bool:
    return user_id == settings.ADMIN_ID


def _pausado() -> bool:
    return store.get_config(CHAVE_PAUSADO) == "1"


def _canal_fixo() -> int:
    """Canal fixo do agente: o definido por comando; se não houver, o AGENT_CHANNEL_ID do .env (0 = nenhum)."""
    valor = store.get_config(CHAVE_CANAL) or ""
    if valor.isdigit():
        return int(valor)
    return settings.AGENT_CANAL_ID


async def configurar_canal(canal_id: int, autor) -> str:
    """Define o canal fixo do agente; chamado pelo fluxo de canais em /setup."""
    store.set_config(CHAVE_CANAL, str(canal_id) if canal_id else "")
    texto = f"{E.correct} Canal fixo do Ghost Agent definido: <#{canal_id}>."
    await notify.registrar(
        "agente_canal_alterado",
        f"{E.information} Canal fixo do Ghost Agent alterado",
        settings.CORES["info"],
        [("Canal", str(canal_id) if canal_id else "removido"), ("Por", f"{autor} ({autor.id})")],
    )
    return texto


def _linhas_diario() -> str:
    return (
        "\n".join(diario.rotulo(e) for e in diario.ultimas(15))
        or f"{E.information} O diário está vazio."
    )


def _montar_desfazer(numero: int, autor_id: int) -> tuple[str, str | None]:
    """Monta o plano de reversão. Nada é desfeito aqui: só vira plano para o dono aprovar."""
    erro = diario.conferir_desfazer({"id": numero})
    if erro:
        return f"{E.wrong} {erro[0].upper() + erro[1:]}.", None
    acao = {
        "ferramenta": "desfazer_acao",
        "args": {"id": numero},
        "resumo": ferramentas.FERRAMENTAS["desfazer_acao"].resumo({"id": numero}),
        "perigo": "medio",
    }
    ident = loop.plano_direto([acao], autor_id)
    return (
        f"Plano para desfazer. Aprove se for isso:\n{loop.linhas_do_plano(loop.PLANOS[ident])}",
        ident,
    )


def _iniciar(canal, dono) -> str:
    loop.abrir(canal, dono)
    conversa.definir_ligada(canal.id, True)  # fica ligado mesmo se o bot reiniciar
    return f"{E.correct} Ghost Agent ligado neste canal. Fala normal; use `/agente controle` para encerrar a sessão."


def _encerrar(canal_id: int) -> str:
    estava_ligada = conversa.ligada(canal_id)
    tinha_sessao = loop.fechar(canal_id)
    conversa.definir_ligada(canal_id, False)
    if not (estava_ligada or tinha_sessao):
        return f"{E.information} Não tem sessão do Ghost Agent neste canal."
    return f"{E.correct} Sessão encerrada. O histórico fica guardado; `/agente novo` começa do zero."


async def _receber(canal, texto: str, dono) -> None:
    if _pausado():
        return
    modelo = provedor.do_ambiente()
    if modelo is None:
        await canal.send(
            f"{E.wrong} Modelo do Ghost Agent não configurado: confira AGENT_PROVIDER e AGENT_API_KEY no .env.",
            delete_after=10,
        )
        return
    await loop.receber(canal.id, texto, modelo, store.get_config("agente_estilo"), dono)


class Agente(commands.Cog):
    grupo_agente = app_commands.Group(name="agente", description="[ dono ]")

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @grupo_agente.command(name="controle", description="[ dono ] Inicia/encerra sessão ou pausa/retoma globalmente")
    @app_commands.describe(acao="Ação do agente")
    @app_commands.choices(acao=[
        app_commands.Choice(name="iniciar sessão neste canal", value="iniciar"),
        app_commands.Choice(name="encerrar sessão neste canal", value="encerrar"),
        app_commands.Choice(name="pausar em todos os canais", value="pausar"),
        app_commands.Choice(name="retomar em todos os canais", value="retomar"),
    ])
    async def agente_controle(self, interaction: discord.Interaction, acao: app_commands.Choice[str]) -> None:
        if not _dono(interaction.user.id):
            await interaction.response.send_message(f"{E.wrong} Só o dono usa o Ghost Agent.", ephemeral=True)
            return
        if acao.value == "iniciar":
            if interaction.guild is None:
                await interaction.response.send_message("Use o agente em um canal do servidor.", ephemeral=True)
                return
            store.set_config(CHAVE_PAUSADO, "0")
            resposta = _iniciar(interaction.channel, interaction.user)
        elif acao.value == "encerrar":
            resposta = _encerrar(interaction.channel_id)
        elif acao.value == "pausar":
            store.set_config(CHAVE_PAUSADO, "1")
            for canal_id in tuple(loop.SESSOES):
                loop.fechar(canal_id)
            resposta = f"{E.correct} Ghost Agent pausado globalmente."
        else:
            store.set_config(CHAVE_PAUSADO, "0")
            resposta = f"{E.correct} Ghost Agent retomado globalmente."
        await interaction.response.send_message(resposta, ephemeral=True)

    @grupo_agente.command(name="estilo", description="[ dono ] Define ou restaura o estilo de resposta")
    @app_commands.describe(texto="Como o agente deve falar; use 'padrão' para limpar")
    async def agente_estilo(self, interaction: discord.Interaction, texto: str) -> None:
        if not _dono(interaction.user.id):
            await interaction.response.send_message(f"{E.wrong} Só o dono usa o Ghost Agent.", ephemeral=True)
            return
        if texto.strip().casefold() in {"padrão", "padrao"}:
            store.set_config("agente_estilo", "")
            await interaction.response.send_message(f"{E.correct} Estilo padrão restaurado.", ephemeral=True)
            return
        guardado = prompts.limpar_estilo(texto)
        store.set_config("agente_estilo", guardado)
        await interaction.response.send_message(f"{E.correct} Estilo salvo ({len(guardado)} caracteres).", ephemeral=True)

    @grupo_agente.command(name="permissao", description="[ dono ] Define se uma ferramenta roda direto, pergunta ou é negada")
    @app_commands.describe(ferramenta="Nome da ferramenta (ex.: apagar_canal)", acao="permitir, perguntar ou negar")
    @app_commands.choices(acao=[app_commands.Choice(name=p, value=p) for p in ferramentas.POLITICAS])
    async def agente_permissao(
        self, interaction: discord.Interaction, ferramenta: str, acao: app_commands.Choice[str]
    ) -> None:
        if not _dono(interaction.user.id):
            await interaction.response.send_message(f"{E.wrong} Só o dono usa o Ghost Agent.", ephemeral=True)
            return
        try:
            ferramentas.definir_politica(ferramenta, acao.value)
        except ValueError:
            await interaction.response.send_message(
                f"{E.wrong} Ferramenta desconhecida. Veja os nomes com `/agente permissoes`.", ephemeral=True)
            return
        await notify.registrar(
            "agente_politica_alterada",
            f"{E.information} Permissão do Ghost Agent alterada",
            settings.CORES["info"],
            [("Ferramenta", ferramenta), ("Nova política", acao.value),
             ("Por", f"{interaction.user} ({interaction.user.id})")],
        )
        await interaction.response.send_message(f"{E.correct} `{ferramenta}`: {acao.value}.", ephemeral=True)

    @agente_permissao.autocomplete("ferramenta")
    async def agente_permissao_autocomplete(self, interaction: discord.Interaction, atual: str):
        termo = atual.lower()
        return [app_commands.Choice(name=n, value=n) for n in ferramentas.FERRAMENTAS if termo in n][:25]

    @grupo_agente.command(name="permissoes", description="[ dono ] Mostra a política de cada ferramenta")
    async def agente_permissoes(self, interaction: discord.Interaction) -> None:
        if not _dono(interaction.user.id):
            await interaction.response.send_message(f"{E.wrong} Só o dono usa o Ghost Agent.", ephemeral=True)
            return
        linhas = [f"`{nome}`: {politica}" for nome, politica in ferramentas.politicas_atuais().items()]
        await interaction.response.send_message("\n".join(linhas)[:1900], ephemeral=True)

    @grupo_agente.command(name="novo", description="[ dono ] Apaga o histórico deste canal (segue ligado)")
    async def agente_novo(self, interaction: discord.Interaction) -> None:
        if not _dono(interaction.user.id):
            await interaction.response.send_message(f"{E.wrong} Só o dono usa o Ghost Agent.", ephemeral=True)
            return
        loop.zerar(interaction.channel_id)
        await interaction.response.send_message(f"{E.correct} Histórico apagado. Começamos do zero.", ephemeral=True)

    @grupo_agente.command(name="contexto", description="[ dono ] Arquivo com regras que o agente segue sempre")
    @app_commands.describe(arquivo="Arquivo .txt ou .md com as regras (até 20 mil caracteres)",
                           remover="Apaga o contexto guardado")
    async def agente_contexto(self, interaction: discord.Interaction,
                              arquivo: discord.Attachment | None = None, remover: bool = False) -> None:
        if not _dono(interaction.user.id):
            await interaction.response.send_message(f"{E.wrong} Só o dono usa o Ghost Agent.", ephemeral=True)
            return
        if remover:
            store.set_config(loop.CHAVE_CONTEXTO, "")
            await interaction.response.send_message(f"{E.correct} Contexto removido.", ephemeral=True)
            return
        if arquivo is None:
            atual = store.get_config(loop.CHAVE_CONTEXTO) or ""
            await interaction.response.send_message(
                f"{E.information} Contexto atual: {len(atual)} caracteres. Mande um arquivo para trocar, "
                "ou `remover:True` para apagar.", ephemeral=True)
            return
        if arquivo.size > 20_000:
            await interaction.response.send_message(f"{E.wrong} Arquivo grande demais (máximo 60 KB).", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            texto = (await arquivo.read()).decode("utf-8").strip()[: prompts.MAX_CONTEXTO]
        except UnicodeDecodeError:
            await interaction.followup.send(f"{E.wrong} O arquivo precisa estar em texto (UTF-8).", ephemeral=True)
            return
        store.set_config(loop.CHAVE_CONTEXTO, texto)
        await notify.registrar(
            "agente_contexto_alterado",
            f"{E.information} Contexto do Ghost Agent alterado",
            settings.CORES["info"],
            [("Tamanho", f"{len(texto)} caracteres"), ("Por", f"{interaction.user} ({interaction.user.id})")],
        )
        await interaction.followup.send(
            f"{E.correct} Contexto salvo ({len(texto)} caracteres). Ele entra em toda conversa, "
            "então quanto menor, mais barato.", ephemeral=True)

    @grupo_agente.command(
        name="diario", description="[ dono ]"
    )
    async def agente_diario(self, interaction: discord.Interaction) -> None:
        if not _dono(interaction.user.id):
            await interaction.response.send_message(
                f"{E.wrong} Só o dono vê o diário do Ghost Agent.", ephemeral=True
            )
            return
        await interaction.response.send_message(_linhas_diario()[:1900], ephemeral=True)

    @grupo_agente.command(
        name="desfazer",
        description="[ dono ]",
    )
    @app_commands.describe(numero="Número da ação no diário (veja com /agente diario)")
    async def agente_desfazer(
        self, interaction: discord.Interaction, numero: int
    ) -> None:
        if not _dono(interaction.user.id):
            await interaction.response.send_message(
                f"{E.wrong} Só o dono desfaz ações do Ghost Agent.", ephemeral=True
            )
            return
        texto, ident = _montar_desfazer(numero, interaction.user.id)
        await interaction.response.send_message(
            texto[:1900],
            ephemeral=True,
            view=loop.confirmacao(ident) if ident else discord.utils.MISSING,
        )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """No canal fixo ou numa sessão ligada, encaminha mensagens normais do dono ao agente."""
        from ...services import incidente

        if message.guild and incidente.ativo(message.guild.id):
            return
        if message.guild is None or message.guild.id != settings.GUILD_ID:
            return
        canal_fixo_id = _canal_fixo()
        canal_fixo = bool(canal_fixo_id) and message.channel.id == canal_fixo_id
        if not (canal_fixo or loop.sessao_ativa(message.channel.id) or conversa.ligada(message.channel.id)):
            return
        if _pausado() or message.author.bot:
            return
        if not _dono(message.author.id):
            loop.observar(
                message.channel.id,
                getattr(message.author, "display_name", str(message.author)),
                message.content,
            )
            return
        pedido = message.content.strip()
        if not pedido or pedido[0] in (">", "/"):
            return
        if not loop.sessao_ativa(message.channel.id):
            loop.abrir(message.channel, message.author)
        await _receber(message.channel, pedido, message.author)

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        """Dono reage na mensagem de aprovação do plano: 👍 (ou ✅, 🚀...) aprova, ❌ (ou 👎, 🛑) cancela."""
        if payload.user_id != settings.ADMIN_ID:
            return
        ident = loop.MENSAGENS_PLANO.get(payload.message_id)
        if ident is None:
            return
        nome = payload.emoji.name or ""
        if any(nome.startswith(r) for r in loop.REACOES_APROVAR):
            ok = True
        elif any(nome.startswith(r) for r in loop.REACOES_CANCELAR):
            ok = False
        else:
            return
        loop.decidir(ident, ok)

    @route(prefix="agente:")
    async def _confirmar(self, interaction: discord.Interaction, cid: str) -> None:
        if not _dono(interaction.user.id):
            await interaction.response.send_message(
                f"{E.wrong} Só o dono confirma ações do Ghost Agent.", ephemeral=True
            )
            return
        partes = cid.split(":")
        if len(partes) != 3 or partes[1] not in ("ok", "no"):
            await interaction.response.send_message(
                f"{E.wrong} Ação desconhecida.", ephemeral=True
            )
            return
        ident, ok = partes[2], partes[1] == "ok"

        # plano de uma tarefa: a própria tarefa aplica e segue
        if loop.decidir(ident, ok):
            await interaction.response.edit_message(
                content=f"{E.reload} Aplicando o plano..."
                if ok
                else f"{E.wrong} Plano cancelado.",
                view=None,
            )
            return

        # plano avulso (ex.: /agente desfazer)
        loop._expirar_planos()
        plano = loop.PLANOS.pop(ident, None)
        if plano is None:
            await interaction.response.edit_message(
                content=f"{E.information} Esse plano já foi resolvido.", view=None
            )
            return
        if not ok:
            await interaction.response.edit_message(
                content=f"{E.wrong} Plano cancelado.", view=None
            )
            return
        await interaction.response.edit_message(
            content=f"{E.reload} Aplicando o plano...", view=None
        )
        resultados = await loop.executar_acoes(
            interaction.guild, plano["acoes"], ident, interaction.user
        )
        await interaction.edit_original_response(
            content=f"{E.correct} Plano aplicado:\n" + "\n".join(resultados)[:1900]
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Agente(bot))
