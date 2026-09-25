"""Filtros de atributo de carta (Tipo, Atributo, Race, Arquétipo, ATK, DEF,
Estrelas, Escala, Link) — compartilhados entre `CardRepository.search()`/
`.count()` e `CollectionRepository.list_filtered()`, e repassados por
`DeckService.searchable_pool()` para a busca do Deck Builder. Um só lugar
para a regra de filtro evita que as três telas de busca de carta (Banco de
Dados, Coleção, Deck Builder) divirjam sobre o que cada campo significa.

Todos os campos vivem em `Card` (`db/tables.py`) — nada aqui depende de
`CardPrint`/coleção, por isso um dataclass simples encadeável em qualquer
`select()` que já tenha `Card` na consulta.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Any

from ..db.tables import Card


@dataclass(frozen=True, slots=True)
class CardAttributeFilters:
    card_type: str | None = None
    attribute: str | None = None
    race: str | None = None
    #: Substring, não igualdade — a lista de arquétipos é grande demais para
    #: um `<select>` (centenas de valores), então a tela usa um campo livre.
    archetype: str | None = None
    atk_min: int | None = None
    atk_max: int | None = None
    def_min: int | None = None
    def_max: int | None = None
    level_min: int | None = None
    level_max: int | None = None
    scale_min: int | None = None
    scale_max: int | None = None
    linkval_min: int | None = None
    linkval_max: int | None = None

    def is_empty(self) -> bool:
        # `slots=True` não gera `__dict__` — precisa iterar por `fields()`.
        return all(getattr(self, f.name) is None for f in fields(self))


def apply_card_attribute_filters(stmt: Any, filters: CardAttributeFilters | None) -> Any:
    """Encadeia `.where()` no `stmt` para cada campo preenchido de `filters`.

    `stmt` precisa ser um `select()` que já tenha `Card` como entidade
    consultável (direto, como em `CardRepository`, ou via `join(Card)`, como
    em `CollectionRepository.list_filtered`). Campos `None` não filtram
    nada — `filters=None`/todo-vazio devolve `stmt` inalterado.
    """
    if filters is None:
        return stmt
    if filters.card_type:
        stmt = stmt.where(Card.type == filters.card_type)
    if filters.attribute:
        stmt = stmt.where(Card.attribute == filters.attribute)
    if filters.race:
        stmt = stmt.where(Card.race == filters.race)
    if filters.archetype:
        stmt = stmt.where(Card.archetype.ilike(f"%{filters.archetype.strip()}%"))
    if filters.atk_min is not None:
        stmt = stmt.where(Card.atk >= filters.atk_min)
    if filters.atk_max is not None:
        stmt = stmt.where(Card.atk <= filters.atk_max)
    if filters.def_min is not None:
        stmt = stmt.where(Card.defense >= filters.def_min)
    if filters.def_max is not None:
        stmt = stmt.where(Card.defense <= filters.def_max)
    if filters.level_min is not None:
        stmt = stmt.where(Card.level >= filters.level_min)
    if filters.level_max is not None:
        stmt = stmt.where(Card.level <= filters.level_max)
    if filters.scale_min is not None:
        stmt = stmt.where(Card.scale >= filters.scale_min)
    if filters.scale_max is not None:
        stmt = stmt.where(Card.scale <= filters.scale_max)
    if filters.linkval_min is not None:
        stmt = stmt.where(Card.linkval >= filters.linkval_min)
    if filters.linkval_max is not None:
        stmt = stmt.where(Card.linkval <= filters.linkval_max)
    return stmt
