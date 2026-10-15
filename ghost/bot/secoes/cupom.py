"""[ admin ] Cupons: listar, criar, editar, ligar ou desligar, apagar e regras de uso.

Regras que a equipe configura: desconto em % ou em R$, limite de usos (total e por pessoa), validade, só primeira
compra, mínimo de compras aprovadas, valor mínimo do produto, e cargos e usuários que podem ou não usar.
Lista vazia de cargos ou usuários permitidos quer dizer "todos". Um cargo ou usuário bloqueado vence a permissão.
A regra de cada campo fica em ghost/data/coupons.py (editar e avaliar).
"""

import math
import logging
import re
import sqlite3
from datetime import date

import discord

from ...ui import layout
from ...catalogo import render
from ...data import coupons
from ...ui.emojis import E
from ...services import notify, rest
from ...config.settings import CORES
from . import (
    aviso,
    botao_ligado,
    confirmar,
    navegacao,
    pagina_de,
    painel,
    rodape_pagina,
    seletor_entidade,
    seletor_opcoes,
    so_admin,
    sem_acao,
    voltar,
)

log = logging.getLogger("secao.cupom")

NOME_CUPOM = re.compile(r"^[A-Za-z0-9_-]{1,20}$")
ITENS_POR_PAGINA = 25  # limite de opções de um select

# campo do banco -> (tipo de seletor, rótulo na tela)
REGRAS = {
    "cargos_permitidos": ("cargo", "Cargos que PODEM usar (vazio = todos)"),
    "cargos_bloqueados": ("cargo", "Cargos que NÃO podem usar"),
    "usuarios_permitidos": ("usuario", "Usuários que PODEM usar (vazio = todos)"),
    "usuarios_bloqueados": ("usuario", "Usuários que NÃO podem usar"),
}


# ── Leitura e formatação ──────────────────────────────────────────────────────


def _num(alvo: list[str], pos: int = 0) -> int:
    return int(alvo[pos]) if len(alvo) > pos and alvo[pos].isdigit() else 0


def _id(texto: str | None) -> int | None:
    return int(texto) if texto and texto.isdigit() else None


def _descricao_desconto(c: dict) -> str:
    if c["tipo_desconto"] == "valor":
        return f"{render.brl(float(c['desconto_valor'] or 0))} de desconto"
    return f"{int(c['desconto_pct'] or 0)}%"


def _mencoes(ids: list[str], prefixo: str, vazio: str) -> str:
    return " ".join(f"<{prefixo}{i}>" for i in ids) or vazio


# ── Leitura de formulário ─────────────────────────────────────────────────────


def _nome(texto: str) -> str:
    nome = texto.strip().upper()
    if not NOME_CUPOM.match(nome):
        raise ValueError(
            "O código aceita de 1 a 20 letras, números, - e _, sem espaço."
        )
    return nome


def _desconto(texto: str) -> tuple[str, int | None, float | None]:
    bruto = texto.strip().lower().replace(" ", "")
    if bruto.endswith("%"):
        numero = bruto[:-1]
        if not numero.isdigit() or not 1 <= int(numero) <= 99:
            raise ValueError("Porcentagem de 1 a 99, como 10%.")
        return "pct", int(numero), None
    bruto = bruto.replace("r$", "").replace(",", ".")
    try:
        valor = round(float(bruto), 2)
    except ValueError:
        raise ValueError(
            "Desconto inválido. Use 10% para porcentagem ou R$ 5,00 para valor."
        ) from None
    if not math.isfinite(valor):
        raise ValueError(
            "Desconto inválido. Use 10% para porcentagem ou R$ 5,00 para valor."
        )
    if valor <= 0:
        raise ValueError("O desconto em valor precisa ser maior que zero.")
    return "valor", None, valor


def _inteiro(texto: str, rotulo: str, minimo: int = 1) -> int | None:
    bruto = texto.strip()
    if not bruto:
        return None
    if not bruto.isdigit() or int(bruto) < minimo:
        raise ValueError(
            f"{rotulo}: use um número inteiro a partir de {minimo}, ou deixe vazio."
        )
    return int(bruto)


def _dinheiro(texto: str, rotulo: str) -> float:
    bruto = texto.strip().lower().replace("r$", "").replace(",", ".")
    if not bruto:
        return 0.0
    try:
        valor = round(float(bruto), 2)
    except ValueError:
        raise ValueError(
            f"{rotulo}: use um valor como 20 ou 20,50, ou deixe vazio."
        ) from None
    if not math.isfinite(valor):
        raise ValueError(f"{rotulo}: valor inválido.")
    if valor < 0:
        raise ValueError(f"{rotulo} não pode ser negativo.")
    return valor


def _validade(texto: str, futura: bool) -> str | None:
    bruto = texto.strip()
    if not bruto:
        return None
    try:
        data = date.fromisoformat(bruto)
    except ValueError:
        raise ValueError(
            "Validade no formato AAAA-MM-DD, como 2026-12-31, ou deixe vazio."
        ) from None
    if futura and data < date.today():
        raise ValueError("A validade já passou. Escolha uma data de hoje em diante.")
    return data.isoformat()


async def _erro(interaction: discord.Interaction, exc: Exception) -> None:
    await interaction.response.send_message(f"{E.wrong} {exc}", ephemeral=True)


async def _avisar(
    interaction: discord.Interaction, titulo: str, campos: list[tuple[str, str]]
) -> None:
    await notify.registrar(
        "config_alterada",
        f"{E.coupon} {titulo}",
        CORES["info"],
        [*campos, ("Por", f"{interaction.user} ({interaction.user.id})")],
    )


# ── Modais ────────────────────────────────────────────────────────────────────


class CriarCupomModal(discord.ui.Modal):
    def __init__(self):
        super().__init__(title="Novo cupom")
        self.nome = discord.ui.TextInput(label="Código (ex.: GHOST10)", max_length=20)
        self.desconto = discord.ui.TextInput(
            label="Desconto: 10% ou R$ 5,00", max_length=20
        )
        self.limite = discord.ui.TextInput(
            label="Limite de usos (vazio = sem limite)", required=False, max_length=9
        )
        self.validade = discord.ui.TextInput(
            label="Validade AAAA-MM-DD (vazio = sem)", required=False, max_length=10
        )
        self.minimo = discord.ui.TextInput(
            label="Valor mínimo do produto (vazio = sem)", required=False, max_length=12
        )
        for campo in (
            self.nome,
            self.desconto,
            self.limite,
            self.validade,
            self.minimo,
        ):
            self.add_item(campo)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        from ..security import authorize

        if not await authorize(interaction, "admin"):
            return
        try:
            nome = _nome(self.nome.value)
            tipo, pct, valor = _desconto(self.desconto.value)
            limite = _inteiro(self.limite.value, "Limite de usos")
            validade = _validade(self.validade.value, futura=True)
            minimo = _dinheiro(self.minimo.value, "Valor mínimo")
            cid = coupons.criar(
                nome,
                desconto_pct=pct,
                desconto_valor=valor,
                limite_usos=limite,
                validade=validade,
                min_produto=minimo,
            )
        except ValueError as exc:
            await _erro(interaction, exc)
            return
        except sqlite3.IntegrityError:
            await _erro(
                interaction,
                ValueError(
                    "Não consegui criar: o código já existe ou um valor está fora do limite."
                ),
            )
            return
        await _avisar(
            interaction,
            "Cupom criado",
            [("Cupom", nome), ("Desconto", _descricao_desconto(coupons.get(cid)))],
        )
        await rest.responder(
            interaction,
            detalhe(cid, confirmacao=f"Cupom `{nome}` criado."),
            editar=True,
        )


class EditarCupomModal(discord.ui.Modal):
    def __init__(self, c: dict):
        super().__init__(title=f"Editar {c['nome']}"[:45])
        self.cid = c["id"]
        self.nome = discord.ui.TextInput(
            label="Código", default=c["nome"], max_length=20
        )
        atual = _descricao_desconto(c).replace(" de desconto", "")
        self.desconto = discord.ui.TextInput(
            label="Desconto: 10% ou R$ 5,00", default=atual, max_length=20
        )
        self.limite = discord.ui.TextInput(
            label="Limite de usos (vazio = sem limite)",
            required=False,
            default=str(c["limite_usos"] or ""),
            max_length=9,
        )
        self.por_pessoa = discord.ui.TextInput(
            label="Usos por pessoa (vazio = sem limite)",
            required=False,
            default=str(c["limite_por_usuario"] or ""),
            max_length=9,
        )
        self.validade = discord.ui.TextInput(
            label="Validade AAAA-MM-DD (vazio = sem)",
            required=False,
            default=c["validade"] or "",
            max_length=10,
        )
        for campo in (
            self.nome,
            self.desconto,
            self.limite,
            self.por_pessoa,
            self.validade,
        ):
            self.add_item(campo)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        from ..security import authorize

        if not await authorize(interaction, "admin"):
            return
        try:
            nome = _nome(self.nome.value)
            existente = coupons.get(nome)
            if existente and existente["id"] != self.cid:
                raise ValueError("Já existe outro cupom com esse código.")
            tipo, pct, valor = _desconto(self.desconto.value)
            campos = dict(
                nome=nome,
                tipo_desconto=tipo,
                desconto_pct=pct,
                desconto_valor=valor,
                limite_usos=_inteiro(self.limite.value, "Limite de usos"),
                limite_por_usuario=_inteiro(self.por_pessoa.value, "Usos por pessoa"),
                validade=_validade(self.validade.value, futura=False),
            )
            coupons.editar(self.cid, **campos)
        except ValueError as exc:
            await _erro(interaction, exc)
            return
        except sqlite3.IntegrityError:
            await _erro(
                interaction, ValueError("Não consegui salvar: o código já existe.")
            )
            return
        await _avisar(interaction, "Cupom alterado", [("Cupom", nome)])
        await rest.responder(
            interaction, detalhe(self.cid, confirmacao="Cupom salvo."), editar=True
        )


class MinimosModal(discord.ui.Modal):
    def __init__(self, c: dict):
        super().__init__(title=f"Mínimos de {c['nome']}"[:45])
        self.cid = c["id"]
        self.compras = discord.ui.TextInput(
            label="Compras aprovadas mínimas (vazio = 0)",
            required=False,
            default=str(c["min_compras"] or ""),
            max_length=6,
        )
        self.produto = discord.ui.TextInput(
            label="Valor mínimo do produto (vazio = 0)",
            required=False,
            default=str(c["min_produto"] or ""),
            max_length=12,
        )
        self.add_item(self.compras)
        self.add_item(self.produto)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        from ..security import authorize

        if not await authorize(interaction, "admin"):
            return
        try:
            compras = _inteiro(self.compras.value, "Compras mínimas", minimo=0) or 0
            minimo = _dinheiro(self.produto.value, "Valor mínimo")
            coupons.editar(self.cid, min_compras=compras, min_produto=minimo)
        except ValueError as exc:
            await _erro(interaction, exc)
            return
        await rest.responder(
            interaction, detalhe(self.cid, confirmacao="Mínimos salvos."), editar=True
        )


# ── Telas ─────────────────────────────────────────────────────────────────────


def menu(confirmacao: str | None = None, pagina: int = 0) -> dict:
    todos = coupons.listar()
    itens = []
    if confirmacao:
        itens.append(aviso(confirmacao))
    if not todos:
        itens.append(layout.texto("Nenhum cupom cadastrado. Crie um para começar."))
    else:
        fatia, pagina, total = pagina_de(todos, pagina, ITENS_POR_PAGINA)
        opcoes = [
            {
                "label": c["nome"][:100],
                "value": str(c["id"]),
                "description": f"{_descricao_desconto(c)} · {'ligado' if c['ativo'] else 'desligado'} · "
                f"{c['usos_atuais']} uso(s)"[:100],
            }
            for c in fatia
        ]
        itens.append(
            layout.texto(
                "Escolha um cupom para ver, editar, ligar ou desligar.\n"
                + rodape_pagina(pagina, total, f"{len(todos)} cupom(ns)")
            )
        )
        itens.append(seletor_opcoes("ger:cupom:pick", "Escolha um cupom...", opcoes))
        nav = navegacao("ger:cupom:lista", pagina, total)
        if nav:
            itens.append(nav)
    itens.append(
        layout.linha(
            render.botao("Novo cupom", "ger:cupom:novo", 3, "@plus"),
            render.botao("Voltar", "ger:menu", 2, "@reload"),
        )
    )
    return painel(f"{E.coupon} Cupons", *itens)


def detalhe(cid: int, confirmacao: str | None = None) -> dict:
    c = coupons.get(cid)
    if c is None:
        return menu(confirmacao="Esse cupom não existe mais.")
    usos = f"{c['usos_atuais']}" + (
        f" de {c['limite_usos']}" if c["limite_usos"] else ""
    )
    itens = []
    if confirmacao:
        itens.append(aviso(confirmacao))
    itens.append(
        layout.texto(
            f"**Desconto:** {_descricao_desconto(c)}\n"
            f"**Usos:** {usos} · **Por pessoa:** {c['limite_por_usuario'] or 'sem limite'}\n"
            f"**Validade:** {c['validade'] or 'sem validade'}\n"
            f"**Compras aprovadas mínimas:** {c['min_compras']} · **Valor mínimo do produto:** "
            f"{render.brl(c['min_produto']) if c['min_produto'] else 'sem mínimo'}\n"
            f"**Cargos que podem:** {_mencoes(c['cargos_permitidos'], '@&', 'todos')}\n"
            f"**Cargos bloqueados:** {_mencoes(c['cargos_bloqueados'], '@&', 'nenhum')}\n"
            f"**Usuários que podem:** {_mencoes(c['usuarios_permitidos'], '@', 'todos')}\n"
            f"**Usuários bloqueados:** {_mencoes(c['usuarios_bloqueados'], '@', 'nenhum')}\n"
            f"-# Código `{c['nome']}` · cupom #{c['id']}"
        )
    )
    itens.append(
        layout.linha(
            botao_ligado(bool(c["ativo"]), "Cupom", f"ger:cupom:alternar:{cid}"),
            render.botao("Editar valores", f"ger:cupom:editar:{cid}", 1, "@save"),
            render.botao("Mínimos", f"ger:cupom:minimos:{cid}", 2, "@cardbox"),
            botao_ligado(
                bool(c["primeira_compra"]),
                "Só primeira compra",
                f"ger:cupom:primeira:{cid}",
            ),
            render.botao("Apagar", f"ger:cupom:apagar:{cid}", 4, "@delete"),
        )
    )
    for campo, (tipo, rotulo) in REGRAS.items():
        itens.append(
            seletor_entidade(
                tipo,
                f"ger:cupom:regra:{campo}:{cid}",
                rotulo,
                minimo=0,
                maximo=25,
                padrao=c[campo],
            )
        )
    itens.append(voltar("ger:cupom:lista:0", "Cupons"))
    return painel(f"{E.coupon} Cupom {c['nome']}", *itens)


def apagar_confirmar(cid: int) -> dict:
    c = coupons.get(cid)
    nome = c["nome"] if c else str(cid)
    return confirmar(
        f"Apagar {nome}",
        f"Isso apaga o cupom `{nome}` e o histórico de usos dele. Não dá para desfazer.",
        f"ger:cupom:apagar_ok:{cid}",
        f"ger:cupom:abrir:{cid}",
        "Apagar de vez",
    )


# ── Ações ─────────────────────────────────────────────────────────────────────


async def tratar(interaction: discord.Interaction, acao: str, alvo: list[str]) -> None:
    if not await so_admin(interaction):
        return
    if acao in ("", "menu"):
        return await rest.responder(interaction, menu(), editar=True)
    if acao == "lista":
        return await rest.responder(interaction, menu(pagina=_num(alvo)), editar=True)
    if acao == "novo":
        return await interaction.response.send_modal(CriarCupomModal())
    if acao == "pick":
        valores = interaction.data.get("values") or []
        cid = _id(valores[0]) if valores else None
        if cid is None or coupons.get(cid) is None:
            return await sem_acao(interaction)
        return await rest.responder(interaction, detalhe(cid), editar=True)
    if acao == "regra":
        campo = alvo[0] if alvo else ""
        cid = _id(alvo[1]) if len(alvo) > 1 else None
        if campo not in REGRAS or cid is None or coupons.get(cid) is None:
            return await sem_acao(interaction)
        valores = list(interaction.data.get("values") or [])
        coupons.editar(cid, **{campo: valores})
        await _avisar(
            interaction,
            "Regra de cupom alterada",
            [
                ("Cupom", coupons.get(cid)["nome"]),
                ("Regra", REGRAS[campo][1]),
                ("Quantidade", str(len(valores))),
            ],
        )
        return await rest.responder(
            interaction, detalhe(cid, confirmacao="Regra salva."), editar=True
        )

    cid = _id(alvo[0]) if alvo else None
    c = coupons.get(cid) if cid is not None else None
    if c is None:
        return await rest.responder(
            interaction, menu(confirmacao="Esse cupom não existe mais."), editar=True
        )
    if acao == "abrir":
        return await rest.responder(interaction, detalhe(cid), editar=True)
    if acao == "alternar":
        ligado = coupons.alternar(cid)
        await _avisar(
            interaction,
            "Cupom ligado ou desligado",
            [("Cupom", c["nome"]), ("Estado", "ligado" if ligado else "desligado")],
        )
        return await rest.responder(
            interaction,
            detalhe(cid, confirmacao=f"Cupom {'ligado' if ligado else 'desligado'}."),
            editar=True,
        )
    if acao == "primeira":
        coupons.editar(cid, primeira_compra=not c["primeira_compra"])
        return await rest.responder(
            interaction,
            detalhe(cid, confirmacao="Regra de primeira compra salva."),
            editar=True,
        )
    if acao == "editar":
        return await interaction.response.send_modal(EditarCupomModal(c))
    if acao == "minimos":
        return await interaction.response.send_modal(MinimosModal(c))
    if acao == "apagar":
        return await rest.responder(interaction, apagar_confirmar(cid), editar=True)
    if acao == "apagar_ok":
        coupons.apagar(cid)
        await _avisar(interaction, "Cupom apagado", [("Cupom", c["nome"])])
        return await rest.responder(
            interaction, menu(confirmacao=f"Cupom `{c['nome']}` apagado."), editar=True
        )
    return await sem_acao(interaction)
