"""Eventos que podem ir ao canal de logs. As definições ficam em eventos.json (validadas ao carregar);
a equipe liga ou desliga cada um pelo /logs, e a escolha fica no banco, sobrescrevendo o padrão."""

import json
from dataclasses import dataclass
from pathlib import Path

from ..data import store

_ARQUIVO = Path(__file__).resolve().parent.parent / "config" / "eventos.json"
MAX_POR_CATEGORIA = 20


class EventosInvalidos(Exception):
    pass


@dataclass(frozen=True)
class Evento:
    id: str
    categoria: str
    nome: str
    padrao: bool


def _carregar() -> tuple[dict[str, str], dict[str, Evento]]:
    dados = json.loads(_ARQUIVO.read_text(encoding="utf-8"))
    categorias = dict(dados["categorias"])
    eventos: dict[str, Evento] = {}
    for item in dados["eventos"]:
        evento = Evento(item["id"], item["categoria"], item["nome"], item["padrao"])
        if evento.id in eventos:
            raise EventosInvalidos(f"evento duplicado: {evento.id}")
        if evento.categoria not in categorias:
            raise EventosInvalidos(
                f"categoria desconhecida em {evento.id}: {evento.categoria}"
            )
        if not isinstance(evento.padrao, bool):
            raise EventosInvalidos(f"padrão de {evento.id} deve ser true ou false")
        eventos[evento.id] = evento
    for categoria in categorias:
        if (
            sum(1 for e in eventos.values() if e.categoria == categoria)
            > MAX_POR_CATEGORIA
        ):
            raise EventosInvalidos(
                f"categoria {categoria} tem mais de {MAX_POR_CATEGORIA} eventos"
            )
    return categorias, eventos


CATEGORIAS, EVENTOS = _carregar()


def ativo(evento_id: str) -> bool:
    """Id desconhecido levanta KeyError de propósito: é erro de programação."""
    padrao = EVENTOS[evento_id].padrao
    valor = store.get_config(f"log:{evento_id}")
    return padrao if valor is None else valor == "1"


def definir(evento_id: str, ligado: bool) -> None:
    store.set_config(f"log:{evento_id}", "1" if ligado else "0")


def da_categoria(categoria: str) -> list[Evento]:
    return [e for e in EVENTOS.values() if e.categoria == categoria]


def restaurar_padrao(categoria: str) -> None:
    for evento in da_categoria(categoria):
        definir(evento.id, evento.padrao)
