"""[ dono ] Cargos automáticos por nível, em três critérios: valor comprado, compras aprovadas e mensagens no servidor.

- Valor comprado: a lista de sempre (store.niveis_cargo / store.salvar_niveis, chave cargos_niveis). O nível de
  R$ 0 é o cargo de cliente: existe sempre e não pode ser apagado.
- Compras e mensagens: guardados em config (cargos_niveis_compras e cargos_niveis_mensagens), no mesmo formato
  {"valor": limite, "cargo": nome}, com o limite inteiro.
- Mensagens: a contagem fica na tabela mensagens_membros. Quem a alimenta é registrar_mensagem, que precisa ser
  chamado a cada mensagem do servidor (ver relatório: ainda não está ligado ao on_message).
- sincronizar dá o cargo do maior nível de compras e de mensagens alcançado. Os cargos de valor continuam com
  services.notify.sync_client_roles.
"""

import json
import logging

import discord

from ...ui import layout
from ...catalogo import render
from ...data import store
from ...database.connection import connection
from ...ui.emojis import E
from ...services import notify, papeis, rest
from ...config.settings import CORES
from . import aviso, confirmar, painel, seletor_opcoes, so_dono, sem_acao, voltar

log = logging.getLogger("secao.cargos")

TIPOS = {  # chave -> (nome na tela, unidade)
    "valor": ("Por valor comprado", "R$"),
    "compras": ("Por compras aprovadas", "compras"),
    "mensagens": ("Por mensagens no servidor", "mensagens"),
}
MAX_NIVEIS = 10
TOLERANCIA = 1e-9

EXPLICACAO = {
    "valor": "Ao atingir um nível, o cliente ganha o cargo e perde os de níveis anteriores. "
    "O nível de R$ 0 é o cargo de cliente e fica sempre.",
    "compras": "Conta compras aprovadas. O cliente fica com o cargo do maior nível que alcançou. "
    "Sem níveis cadastrados, nenhum cargo é dado.",
    "mensagens": "Conta mensagens enviadas no servidor. O cliente fica com o cargo do maior nível que alcançou. "
    "Sem níveis cadastrados, nenhum cargo é dado.",
}


# ── Dados ─────────────────────────────────────────────────────────────────────


def _chave(tipo: str) -> str:
    return f"cargos_niveis_{tipo}"


def niveis(tipo: str) -> list[dict]:
    if tipo == "valor":
        return store.niveis_cargo()
    try:
        bruto = json.loads(store.get_config(_chave(tipo)) or "[]")
    except ValueError:
        log.warning("%s inválido no banco; usando lista vazia", _chave(tipo))
        bruto = []
    return sorted(bruto, key=lambda n: float(n["valor"]))


def _salvar(tipo: str, lista: list[dict]) -> None:
    lista = sorted(lista, key=lambda n: float(n["valor"]))
    if tipo == "valor":
        store.salvar_niveis(lista)
    else:
        store.set_config(_chave(tipo), json.dumps(lista, ensure_ascii=False))


def _restaurar(tipo: str) -> None:
    store.reset_config("cargos_niveis" if tipo == "valor" else _chave(tipo))


def _igual(valor, alvo: float) -> bool:
    return abs(float(valor) - alvo) < TOLERANCIA


def _achar(tipo: str, limite_txt: str) -> dict | None:
    try:
        alvo = float(limite_txt)
    except ValueError:
        return None
    return next((n for n in niveis(tipo) if _igual(n["valor"], alvo)), None)


def _limite(tipo: str, valor) -> str:
    if tipo == "valor":
        return render.brl(float(valor))
    return f"{int(float(valor))} {TIPOS[tipo][1]}"


def _id_limite(valor) -> str:
    return str(float(valor))


def _total_compras(user_id: int) -> int:
    with connection() as conn:
        return conn.execute(
            "SELECT COUNT(*) AS c FROM pedidos WHERE discord_user_id=? AND status='approved'",
            (str(user_id),),
        ).fetchone()["c"]


def registrar_mensagem(user_id: int) -> int:
    """Soma uma mensagem do membro e devolve o total. Chamar a cada mensagem enviada no servidor."""
    with connection() as conn:
        conn.execute(
            "INSERT INTO mensagens_membros (user_id, total) VALUES (?, 1) "
            "ON CONFLICT(user_id) DO UPDATE SET total = total + 1",
            (str(user_id),),
        )
        return conn.execute(
            "SELECT total FROM mensagens_membros WHERE user_id=?", (str(user_id),)
        ).fetchone()["total"]


def total_mensagens(user_id: int) -> int:
    with connection() as conn:
        row = conn.execute(
            "SELECT total FROM mensagens_membros WHERE user_id=?", (str(user_id),)
        ).fetchone()
    return row["total"] if row else 0


async def sincronizar(guild: discord.Guild, user_id: int) -> None:
    """Dá o cargo do maior nível de compras e de mensagens alcançado e tira os cargos de níveis anteriores."""
    try:
        membro = guild.get_member(user_id) or await guild.fetch_member(user_id)
    except discord.HTTPException:
        return
    totais = {"compras": _total_compras(user_id), "mensagens": total_mensagens(user_id)}
    for tipo, total in totais.items():
        lista = niveis(tipo)
        alcancados = [n for n in lista if total >= float(n["valor"])]
        atual = max(alcancados, key=lambda n: float(n["valor"])) if alcancados else None
        tem = {r.id for r in membro.roles}
        for n in lista:
            role = papeis.do_nivel(guild, n)
            if role is None:
                continue
            try:
                if atual and papeis.id_do_nivel(n) == papeis.id_do_nivel(atual):
                    if role.id not in tem:
                        await membro.add_roles(role)
                elif role.id in tem:
                    await membro.remove_roles(role)
            except discord.HTTPException as exc:
                log.warning("falha ao ajustar cargo %s: %s", role.id, exc)


# ── Validação e gravação ──────────────────────────────────────────────────────


def _dinheiro(texto: str) -> float:
    bruto = texto.strip().replace("R$", "").replace("r$", "").replace(",", ".").strip()
    try:
        valor = round(float(bruto), 2)
    except ValueError:
        raise ValueError("Valor inválido. Use números, como 20 ou 0,5.") from None
    if not 0 <= valor < 1_000_000:
        raise ValueError("Valor fora do intervalo permitido.")
    return valor


def _inteiro(texto: str) -> int:
    bruto = texto.strip()
    if not bruto.isdigit() or not 1 <= int(bruto) <= 1_000_000_000:
        raise ValueError("Use um número inteiro a partir de 1.")
    return int(bruto)


def _validar(
    tipo: str,
    limite_txt: str,
    cargo: str,
    atual: float | None,
    guild: discord.Guild | None,
) -> tuple[float, discord.Role]:
    nome = cargo.strip()
    if not nome or len(nome) > 100:
        raise ValueError("Informe o nome do cargo (até 100 caracteres).")
    if guild is None:
        raise ValueError("Esta ação só funciona dentro do servidor.")
    role = discord.utils.get(guild.roles, name=nome)
    if role is None or role.managed or role.is_default():
        raise ValueError(
            f"Não encontrei um cargo chamado `{nome}` no servidor. Crie com esse nome exato."
        )
    valor = _dinheiro(limite_txt) if tipo == "valor" else _inteiro(limite_txt)
    lista = niveis(tipo)
    if atual is None and tipo == "valor" and valor == 0:
        raise ValueError("O nível de R$ 0 já existe: é o cargo de cliente.")
    if atual is not None and tipo == "valor" and atual == 0 and valor != 0:
        raise ValueError("O nível de R$ 0 é o cargo de cliente e fica em zero.")
    if any(
        _igual(n["valor"], valor) and (atual is None or not _igual(valor, atual))
        for n in lista
    ):
        raise ValueError(f"Já existe um nível a partir de {_limite(tipo, valor)}.")
    for outro_tipo in TIPOS:
        for n in niveis(outro_tipo):
            if outro_tipo == tipo and atual is not None and _igual(n["valor"], atual):
                continue
            if papeis.id_do_nivel(n) == role.id:
                raise ValueError("Esse cargo já está em outro nível.")
    if atual is None and len(lista) >= MAX_NIVEIS:
        raise ValueError(f"O limite é de {MAX_NIVEIS} níveis por critério.")
    return valor, role


def _gravar(tipo: str, valor: float, role: discord.Role, atual: float | None) -> None:
    lista = [n for n in niveis(tipo) if atual is None or not _igual(n["valor"], atual)]
    lista.append(
        {
            "valor": valor if tipo == "valor" else int(valor),
            "cargo": role.name,
            "cargo_id": str(role.id),
        }
    )
    _salvar(tipo, lista)


async def migrar(guild: discord.Guild) -> None:
    """Na inicialização: guarda o ID dos cargos fixos, dá ID aos níveis antigos (que só tinham nome)
    e atualiza o nome guardado de cada nível para o nome atual do cargo."""
    for papel in ("membro", "equipe", "cliente"):
        papeis.fixo(guild, papel)
    for tipo in TIPOS:
        lista = niveis(tipo)
        mudou = False
        for n in lista:
            role = papeis.do_nivel(guild, n)
            if role is None and not n.get("cargo_id"):
                role = discord.utils.get(guild.roles, name=n["cargo"])
                if role is not None:
                    n["cargo_id"] = str(role.id)
                    mudou = True
            if role is not None and n.get("cargo") != role.name:
                n["cargo"] = role.name
                mudou = True
        if mudou:
            _salvar(tipo, lista)
            log.info("níveis de cargo (%s) atualizados com ID", tipo)


def _remover(tipo: str, limite_txt: str) -> dict:
    n = _achar(tipo, limite_txt)
    if n is None:
        raise ValueError("Esse nível não existe mais.")
    if tipo == "valor" and float(n["valor"]) == 0:
        raise ValueError("O cargo de cliente (R$ 0) não pode ser apagado.")
    _salvar(
        tipo, [x for x in niveis(tipo) if not _igual(x["valor"], float(n["valor"]))]
    )
    return n


# ── Telas ─────────────────────────────────────────────────────────────────────


def _quantos(n: int) -> str:
    return "nenhum nível" if n == 0 else ("1 nível" if n == 1 else f"{n} níveis")


def menu(confirmacao: str | None = None) -> dict:
    itens = []
    if confirmacao:
        itens.append(aviso(confirmacao))
    itens.append(
        layout.texto(
            "O bot dá cargos sozinho quando o cliente chega a um nível. "
            "Escolha como o cliente sobe de nível.\n"
            + "\n".join(f"-# {TIPOS[t][0]}: {_quantos(len(niveis(t)))}" for t in TIPOS)
        )
    )
    itens.append(
        layout.linha(
            render.botao(TIPOS["valor"][0], "ger:cargos:tipo:valor", 2, "@cardbox"),
            render.botao(TIPOS["compras"][0], "ger:cargos:tipo:compras", 2, "@cart"),
            render.botao(
                TIPOS["mensagens"][0], "ger:cargos:tipo:mensagens", 2, "@members"
            ),
        )
    )
    itens.append(voltar())
    return painel(f"{E.shield} Cargos", *itens)


def tipo_tela(tipo: str, confirmacao: str | None = None) -> dict:
    lista = niveis(tipo)
    itens = []
    if confirmacao:
        itens.append(aviso(confirmacao))
    if lista:
        corpo = "\n".join(
            f"**{_limite(tipo, n['valor'])}** ou mais → {n['cargo']}"
            + (
                " *(cargo de cliente, sempre mantido)*"
                if tipo == "valor" and float(n["valor"]) == 0
                else ""
            )
            for n in lista
        )
    else:
        corpo = "Nenhum nível cadastrado."
    itens.append(layout.texto(f"{corpo}\n-# {EXPLICACAO[tipo]}"))
    if lista:
        opcoes = [
            {
                "label": n["cargo"][:100],
                "value": _id_limite(n["valor"]),
                "description": f"a partir de {_limite(tipo, n['valor'])}"[:100],
            }
            for n in lista
        ]
        itens.append(
            seletor_opcoes(
                f"ger:cargos:sel:{tipo}",
                "Escolha um nível para editar ou apagar...",
                opcoes,
            )
        )
    itens.append(
        layout.linha(
            render.botao("Novo nível", f"ger:cargos:novo:{tipo}", 3, "@plus"),
            render.botao("Restaurar padrão", f"ger:cargos:padrao:{tipo}", 2, "@reload"),
        )
    )
    itens.append(voltar("ger:cargos:menu", "Cargos"))
    return painel(f"{E.shield} {TIPOS[tipo][0]}", *itens)


def nivel_tela(tipo: str, limite_txt: str, confirmacao: str | None = None) -> dict:
    n = _achar(tipo, limite_txt)
    if n is None:
        return tipo_tela(tipo, confirmacao="Esse nível não existe mais.")
    base = tipo == "valor" and float(n["valor"]) == 0
    itens = []
    if confirmacao:
        itens.append(aviso(confirmacao))
    itens.append(
        layout.texto(
            f"**A partir de:** {_limite(tipo, n['valor'])}\n**Cargo:** {n['cargo']}"
            + (
                "\n-# Cargo de cliente: sempre mantido e não pode ser apagado."
                if base
                else ""
            )
        )
    )
    botoes = [
        render.botao(
            "Editar", f"ger:cargos:editar:{tipo}:{_id_limite(n['valor'])}", 1, "@save"
        )
    ]
    if not base:
        botoes.append(
            render.botao(
                "Apagar",
                f"ger:cargos:apagar:{tipo}:{_id_limite(n['valor'])}",
                4,
                "@delete",
            )
        )
    itens.append(layout.linha(*botoes))
    itens.append(voltar(f"ger:cargos:tipo:{tipo}", TIPOS[tipo][0]))
    return painel(f"{E.shield} Nível: {n['cargo']}", *itens)


# ── Modal ─────────────────────────────────────────────────────────────────────


def _texto_limite(tipo: str, valor) -> str:
    if valor is None:
        return ""
    if tipo == "valor":
        return f"{float(valor):.2f}".rstrip("0").rstrip(".").replace(".", ",")
    return str(int(float(valor)))


class NivelModal(discord.ui.Modal):
    def __init__(self, tipo: str, atual: dict | None):
        super().__init__(title=("Editar nível" if atual else "Novo nível"))
        self.tipo = tipo
        self.atual = float(atual["valor"]) if atual else None
        rotulo = (
            "Valor comprado em R$ (ex.: 20)"
            if tipo == "valor"
            else f"Quantidade de {TIPOS[tipo][1]} (número inteiro)"
        )
        self.limite = discord.ui.TextInput(
            label=rotulo[:45], default=_texto_limite(tipo, self.atual), max_length=14
        )
        self.cargo = discord.ui.TextInput(
            label="Nome exato do cargo no servidor",
            max_length=100,
            default=atual["cargo"] if atual else "",
        )
        self.add_item(self.limite)
        self.add_item(self.cargo)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        from ..security import authorize

        if not await authorize(interaction, "owner"):
            return
        try:
            valor, role = _validar(
                self.tipo,
                self.limite.value,
                self.cargo.value,
                self.atual,
                interaction.guild,
            )
            _gravar(self.tipo, valor, role, self.atual)
        except ValueError as exc:
            await interaction.response.send_message(f"{E.wrong} {exc}", ephemeral=True)
            return
        await notify.registrar(
            "config_alterada",
            f"{E.shield} Cargo por {TIPOS[self.tipo][0].lower()}",
            CORES["info"],
            [
                ("Nível", f"{role.name} a partir de {_limite(self.tipo, valor)}"),
                ("Por", f"{interaction.user} ({interaction.user.id})"),
            ],
        )
        await rest.responder(
            interaction, tipo_tela(self.tipo, confirmacao="Nível salvo."), editar=True
        )


# ── Ações ─────────────────────────────────────────────────────────────────────


async def tratar(interaction: discord.Interaction, acao: str, alvo: list[str]) -> None:
    if not await so_dono(interaction):
        return
    if acao in ("", "menu"):
        return await rest.responder(interaction, menu(), editar=True)
    tipo = alvo[0] if alvo else ""
    if tipo not in TIPOS:
        return await sem_acao(interaction)
    if acao == "tipo":
        return await rest.responder(interaction, tipo_tela(tipo), editar=True)
    if acao == "sel":
        valores = interaction.data.get("values") or []
        if not valores:
            return await sem_acao(interaction)
        return await rest.responder(
            interaction, nivel_tela(tipo, valores[0]), editar=True
        )
    if acao == "abrir":
        return await rest.responder(
            interaction, nivel_tela(tipo, alvo[1] if len(alvo) > 1 else ""), editar=True
        )
    if acao == "novo":
        return await interaction.response.send_modal(NivelModal(tipo, None))
    if acao == "editar":
        n = _achar(tipo, alvo[1] if len(alvo) > 1 else "")
        if n is None:
            return await rest.responder(
                interaction,
                tipo_tela(tipo, confirmacao="Esse nível não existe mais."),
                editar=True,
            )
        return await interaction.response.send_modal(NivelModal(tipo, n))
    if acao == "apagar":
        n = _achar(tipo, alvo[1] if len(alvo) > 1 else "")
        if n is None:
            return await rest.responder(
                interaction,
                tipo_tela(tipo, confirmacao="Esse nível não existe mais."),
                editar=True,
            )
        return await rest.responder(
            interaction,
            confirmar(
                f"Apagar nível {n['cargo']}",
                "Quem já tem o cargo continua com ele. O nível só deixa de ser dado a partir de agora.",
                f"ger:cargos:apagar_ok:{tipo}:{_id_limite(n['valor'])}",
                f"ger:cargos:abrir:{tipo}:{_id_limite(n['valor'])}",
                "Apagar de vez",
            ),
            editar=True,
        )
    if acao == "apagar_ok":
        try:
            n = _remover(tipo, alvo[1] if len(alvo) > 1 else "")
        except ValueError as exc:
            return await rest.responder(
                interaction, tipo_tela(tipo, confirmacao=str(exc)), editar=True
            )
        await notify.registrar(
            "config_alterada",
            f"{E.shield} Nível de cargo apagado",
            CORES["aviso"],
            [
                ("Critério", TIPOS[tipo][0]),
                ("Nível", n["cargo"]),
                ("Por", f"{interaction.user} ({interaction.user.id})"),
            ],
        )
        return await rest.responder(
            interaction, tipo_tela(tipo, confirmacao="Nível apagado."), editar=True
        )
    if acao == "padrao":
        _restaurar(tipo)
        await notify.registrar(
            "config_alterada",
            f"{E.shield} Níveis de cargo",
            CORES["info"],
            [
                ("Critério", TIPOS[tipo][0]),
                ("Níveis", "voltaram ao padrão"),
                ("Por", f"{interaction.user} ({interaction.user.id})"),
            ],
        )
        return await rest.responder(
            interaction,
            tipo_tela(tipo, confirmacao="Níveis voltaram ao padrão."),
            editar=True,
        )
    return await sem_acao(interaction)
