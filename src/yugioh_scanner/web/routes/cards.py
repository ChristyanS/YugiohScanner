"""Telas `/cards` — banco de dados completo (lista + galeria) e `/cards/{id}`
(detalhe, plano §10.5).

`/cards` é a Fase 2 do faseamento web pedido pelo usuário: uma aba separada
da coleção, mostrando o catálogo inteiro sincronizado do YGOPRODeck (~14 mil
cartas), não só o que a pessoa já possui. A Fase 3 adiciona idioma: a busca
já casa nome traduzido (FR/DE/IT/PT) em qualquer tela, e tanto a listagem
quanto o detalhe deixam escolher em qual idioma exibir nome e descrição.
"""

from __future__ import annotations

import math
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from ...services.catalog_service import (
    DISPLAY_LANGUAGES,
    resolve_display_language,
    sort_collation_for_language,
)
from ..card_filter_params import parse_card_attribute_filters
from ..deps import CatalogServiceDep, CollectionServiceDep, SettingsServiceDep

router = APIRouter()

#: Tamanho de página menor na galeria: cada card ocupa bem mais espaço visual
#: que uma linha de tabela, então a mesma contagem ficaria comprida demais
#: para rolar. Os dois valores foram escolhidos para fechar bonito num grid
#: `auto-fill` comum (múltiplos de 6/8) sem depender da largura da tela.
_PAGE_SIZE_TABLE = 60
_PAGE_SIZE_GALLERY = 48
#: Visão fichário (§ pedido do usuário): grade fixa 3x3, uma "folha" física
#: de cada vez — o número não é escolha de gosto como os dois de cima, é o
#: próprio layout (CSS grid 3 colunas) que depende dele.
_PAGE_SIZE_BINDER = 9


def _database_filters(request: Request, *, default_lang: str) -> dict[str, Any]:
    q = request.query_params
    view = q.get("view") if q.get("view") in ("table", "gallery", "binder") else "table"
    try:
        page = max(1, int(q.get("page", "1")))
    except ValueError:
        page = 1
    return {
        "search": q.get("q") or None,
        "set_prefix": q.get("set") or None,
        "attrs": parse_card_attribute_filters(q),
        "view": view,
        "page": page,
        "lang": resolve_display_language(q.get("lang"), default=default_lang),
    }


def _load_database_page(
    request: Request, catalog: CatalogServiceDep, settings: SettingsServiceDep
) -> dict[str, Any]:
    filters = _database_filters(request, default_lang=settings.get_default_card_language())
    # `if`/`elif` em vez de um dict de módulo: um dict montado uma vez no
    # import congelaria o valor de `_PAGE_SIZE_GALLERY`/`_PAGE_SIZE_BINDER`
    # daquele momento, e os testes de paginação fazem `monkeypatch.setattr`
    # nessas constantes para forçar poucas páginas sem precisar de massa de
    # dados — precisam ser lidas do módulo a cada chamada.
    if filters["view"] == "gallery":
        page_size = _PAGE_SIZE_GALLERY
    elif filters["view"] == "binder":
        page_size = _PAGE_SIZE_BINDER
    else:
        page_size = _PAGE_SIZE_TABLE

    total = catalog.count_cards(
        filters["search"],
        set_prefix=filters["set_prefix"],
        filters=filters["attrs"],
        lang=filters["lang"],
    )
    total_pages = max(1, math.ceil(total / page_size))
    page = min(filters["page"], total_pages)

    cards = catalog.search_cards(
        filters["search"],
        set_prefix=filters["set_prefix"],
        filters=filters["attrs"],
        limit=page_size,
        offset=(page - 1) * page_size,
        locale=sort_collation_for_language(filters["lang"]),
        lang=filters["lang"],
    )
    display_names = catalog.display_names(cards, filters["lang"])
    return {
        "cards": cards,
        "display_names": display_names,
        "filters": {**filters, "page": page},
        "total": total,
        "total_pages": total_pages,
        "languages": DISPLAY_LANGUAGES,
    }


@router.get("/cards", response_class=HTMLResponse)
def database_page(
    request: Request, catalog: CatalogServiceDep, settings: SettingsServiceDep
) -> HTMLResponse:
    templates = request.app.state.templates
    context = _load_database_page(request, catalog, settings)
    # Só a página cheia precisa das opções dos `<select>` — o form com os
    # filtros não é re-renderizado pelo swap parcial de `/cards/rows`.
    context["filter_options"] = catalog.filter_options()
    return templates.TemplateResponse(request, "database.html", context)


@router.get("/cards/rows", response_class=HTMLResponse)
def database_rows(
    request: Request, catalog: CatalogServiceDep, settings: SettingsServiceDep
) -> HTMLResponse:
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "partials/database_results.html",
        _load_database_page(request, catalog, settings),
    )


@router.get("/cards/{card_id}", response_class=HTMLResponse)
def card_detail(
    card_id: int,
    request: Request,
    catalog: CatalogServiceDep,
    collection: CollectionServiceDep,
    settings: SettingsServiceDep,
) -> HTMLResponse:
    card = catalog.get_card(card_id)
    prints = catalog.prints_for_card(card_id)
    owned = collection.repo.list_for_card(card_id)
    owned_print_ids = {item.card_print_id for item in owned if item.card_print_id is not None}
    total_owned = sum(item.quantity for item in owned)

    alt_names = catalog.alt_names_for_card(card_id)
    lang = resolve_display_language(
        request.query_params.get("lang"), default=settings.get_default_card_language()
    )
    localized = catalog.localize(card, alt_names, lang)
    available_languages = {"EN", *(alt.language for alt in alt_names)}

    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "card_detail.html",
        {
            "card": card,
            "prints": prints,
            "owned": owned,
            "owned_print_ids": owned_print_ids,
            "total_owned": total_owned,
            "localized": localized,
            "lang": lang,
            "languages": DISPLAY_LANGUAGES,
            "available_languages": available_languages,
        },
    )
