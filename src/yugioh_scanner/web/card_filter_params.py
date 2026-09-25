"""Leitura dos filtros avançados de carta (Tipo/Atributo/Race/Arquétipo/ATK/
DEF/Estrelas/Escala/Link) a partir de query string — compartilhado pelas 3
telas que buscam carta (Banco de Dados, Coleção, Deck Builder) para não
reimplementar o mesmo parsing 3 vezes. Espelha o padrão já usado para `page`
em `web/routes/cards.py::_database_filters` (`try/except ValueError` em vez
de 400 — parâmetro de navegação/filtro, não entrada que deva quebrar a
página se vier malformada)."""

from __future__ import annotations

from starlette.datastructures import QueryParams

from ..repositories.card_filters import CardAttributeFilters

#: Nome do parâmetro de URL -> nome do campo em `CardAttributeFilters`. Só os
#: numéricos precisam de mapa (o resto usa o mesmo nome dos dois lados);
#: `link_*` na URL é mais curto que `linkval_*`, que é o nome da coluna real
#: (`Card.linkval`).
_INT_PARAMS = (
    ("atk_min", "atk_min"),
    ("atk_max", "atk_max"),
    ("def_min", "def_min"),
    ("def_max", "def_max"),
    ("level_min", "level_min"),
    ("level_max", "level_max"),
    ("scale_min", "scale_min"),
    ("scale_max", "scale_max"),
    ("link_min", "linkval_min"),
    ("link_max", "linkval_max"),
)


def _parse_int(raw: str | None) -> int | None:
    if raw is None or not raw.strip():
        return None
    try:
        return int(raw.strip())
    except ValueError:
        return None


def parse_card_attribute_filters(query: QueryParams) -> CardAttributeFilters:
    kwargs: dict[str, int | None] = {
        field: _parse_int(query.get(param)) for param, field in _INT_PARAMS
    }
    return CardAttributeFilters(
        card_type=query.get("type") or None,
        attribute=query.get("attribute") or None,
        race=query.get("race") or None,
        archetype=query.get("archetype") or None,
        **kwargs,
    )
