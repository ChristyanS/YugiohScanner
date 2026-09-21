"""Tela `/collection` (plano §10.4).

As ações inline (quantidade, set, remoção) são rotas HTML próprias — devolvem
o fragmento HTMX da linha, não JSON — mas chamam exatamente o mesmo
`CollectionService` que `/api/v1/collection/*`. Nenhuma regra de negócio
mora aqui; só a tradução para fragmento de tabela.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from ...db.tables import CONDITIONS, EDITIONS
from ...exporters import known_profiles
from ...services.catalog_service import (
    DISPLAY_LANGUAGES,
    resolve_display_language,
    sort_collation_for_language,
)
from ..deps import CatalogServiceDep, CollectionServiceDep, SettingsServiceDep

router = APIRouter(prefix="/collection")


def _filters(request: Request, *, default_lang: str) -> dict[str, Any]:
    q = request.query_params
    return {
        "search": q.get("search") or None,
        "set_prefix": q.get("set") or None,
        "no_set": q.get("no_set") in ("1", "true", "on"),
        "no_rarity": q.get("no_rarity") in ("1", "true", "on"),
        "sort": q.get("sort") or "name",
        "descending": q.get("desc") in ("1", "true", "on"),
        "view": q.get("view") if q.get("view") in ("table", "gallery") else "table",
        "lang": resolve_display_language(q.get("lang"), default=default_lang),
    }


def _service_filters(filters: dict[str, Any]) -> dict[str, Any]:
    """Só os filtros que `CollectionService.list_items` conhece — `view` e
    `lang` são detalhe de apresentação da Web, não existem do lado do
    serviço."""
    return {k: v for k, v in filters.items() if k not in ("view", "lang")}


@router.get("", response_class=HTMLResponse)
def collection_page(
    request: Request,
    collection: CollectionServiceDep,
    catalog: CatalogServiceDep,
    settings: SettingsServiceDep,
) -> HTMLResponse:
    filters = _filters(request, default_lang=settings.get_default_card_language())
    items = collection.list_items(
        **_service_filters(filters),
        limit=200,
        locale=sort_collation_for_language(filters["lang"]),
        lang=filters["lang"],
    )
    display_names = catalog.display_names([item.card for item in items], filters["lang"])
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "collection.html",
        {
            "items": items,
            "display_names": display_names,
            "filters": filters,
            "export_profiles": known_profiles("csv"),
            "languages": DISPLAY_LANGUAGES,
            "conditions": CONDITIONS,
            "editions": EDITIONS,
        },
    )


@router.get("/rows", response_class=HTMLResponse)
def collection_rows(
    request: Request,
    collection: CollectionServiceDep,
    catalog: CatalogServiceDep,
    settings: SettingsServiceDep,
) -> HTMLResponse:
    filters = _filters(request, default_lang=settings.get_default_card_language())
    items = collection.list_items(
        **_service_filters(filters),
        limit=200,
        locale=sort_collation_for_language(filters["lang"]),
        lang=filters["lang"],
    )
    display_names = catalog.display_names([item.card for item in items], filters["lang"])
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "partials/collection_results.html",
        {"items": items, "display_names": display_names, "filters": filters},
    )


def _row_response(
    request: Request, catalog: CatalogServiceDep, settings: SettingsServiceDep, item: Any
) -> HTMLResponse:
    lang = resolve_display_language(
        request.query_params.get("lang"), default=settings.get_default_card_language()
    )
    display_name = catalog.display_names([item.card], lang).get(item.card_id, item.card.name)
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "partials/collection_row.html",
        {"item": item, "display_name": display_name, "lang": lang},
    )


@router.post("/{item_id}/quantity", response_class=HTMLResponse)
def adjust_quantity(
    item_id: int,
    request: Request,
    collection: CollectionServiceDep,
    catalog: CatalogServiceDep,
    settings: SettingsServiceDep,
    delta: int = 0,
) -> HTMLResponse:
    item = collection.get_item(item_id)
    new_quantity = max(0, item.quantity + delta)
    collection.set_quantity(item_id, new_quantity)
    if new_quantity == 0:
        return HTMLResponse("")  # a linha some da tabela (hx-swap="outerHTML")
    return _row_response(request, catalog, settings, collection.get_item(item_id))


@router.post("/{item_id}/set-print", response_class=HTMLResponse)
def set_print(
    item_id: int,
    request: Request,
    collection: CollectionServiceDep,
    catalog: CatalogServiceDep,
    settings: SettingsServiceDep,
    card_print_id: Annotated[int, Form()],
) -> HTMLResponse:
    collection.set_print(item_id, card_print_id)
    return _row_response(request, catalog, settings, collection.get_item(item_id))


@router.get("/{item_id}/rarity-options", response_class=HTMLResponse)
def rarity_options(
    item_id: int, request: Request, collection: CollectionServiceDep
) -> HTMLResponse:
    item = collection.get_item(item_id)
    rarities = collection.rarities_for_pending_item(item_id)
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "partials/rarity_options.html",
        {"item": item, "rarities": rarities},
    )


@router.post("/{item_id}/rarity", response_class=HTMLResponse)
def set_rarity(
    item_id: int,
    request: Request,
    collection: CollectionServiceDep,
    catalog: CatalogServiceDep,
    settings: SettingsServiceDep,
    rarity: Annotated[str, Form()],
) -> HTMLResponse:
    collection.resolve_rarity(item_id, rarity)
    return _row_response(request, catalog, settings, collection.get_item(item_id))


#: Sem busca, mostra só os primeiros N prints — com o enriquecimento
#: `yaml-yugi` (ADR 0012) uma carta popular passa de 300 prints, e listar
#: tudo de cara é o problema que a busca resolve (achado real do usuário).
_PRINT_OPTIONS_DEFAULT_LIMIT = 30


@router.get("/{item_id}/print-options", response_class=HTMLResponse)
def print_options(
    item_id: int,
    request: Request,
    collection: CollectionServiceDep,
    catalog: CatalogServiceDep,
    q: str | None = None,
) -> HTMLResponse:
    item = collection.get_item(item_id)
    all_prints = catalog.prints_for_card(item.card_id, query=q)
    total = len(all_prints)
    truncated = q is None and total > _PRINT_OPTIONS_DEFAULT_LIMIT
    prints = all_prints[:_PRINT_OPTIONS_DEFAULT_LIMIT] if truncated else all_prints
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "partials/print_options.html",
        {"item": item, "prints": prints, "total": total, "truncated": truncated, "query": q or ""},
    )


@router.delete("/{item_id}", response_class=HTMLResponse)
def delete_item(item_id: int, collection: CollectionServiceDep) -> HTMLResponse:
    collection.remove(item_id)
    return HTMLResponse("")


@router.delete("", response_class=HTMLResponse)
def clear_collection(
    request: Request,
    collection: CollectionServiceDep,
    settings: SettingsServiceDep,
) -> HTMLResponse:
    collection.clear_all()
    filters = _filters(request, default_lang=settings.get_default_card_language())
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "partials/collection_results.html",
        {"items": [], "display_names": {}, "filters": filters},
    )
