"""Tela `/collection` (plano §10.4).

As ações inline (quantidade, set, remoção) são rotas HTML próprias — devolvem
o fragmento HTMX da linha, não JSON — mas chamam exatamente o mesmo
`CollectionService` que `/api/v1/collection/*`. Nenhuma regra de negócio
mora aqui; só a tradução para fragmento de tabela.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from ...exporters import known_profiles
from ..deps import CatalogServiceDep, CollectionServiceDep

router = APIRouter(prefix="/collection")


def _filters(request: Request) -> dict[str, object]:
    q = request.query_params
    return {
        "search": q.get("search") or None,
        "set_prefix": q.get("set") or None,
        "no_set": q.get("no_set") in ("1", "true", "on"),
        "sort": q.get("sort") or "name",
        "descending": q.get("desc") in ("1", "true", "on"),
        "view": q.get("view") if q.get("view") in ("table", "gallery") else "table",
    }


def _service_filters(filters: dict[str, object]) -> dict[str, object]:
    """Só os filtros que `CollectionService.list_items` conhece — `view` é
    detalhe de apresentação da Web, não existe do lado do serviço."""
    return {k: v for k, v in filters.items() if k != "view"}


@router.get("", response_class=HTMLResponse)
def collection_page(request: Request, collection: CollectionServiceDep) -> HTMLResponse:
    filters = _filters(request)
    items = collection.list_items(**_service_filters(filters), limit=200)  # type: ignore[arg-type]
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "collection.html",
        {
            "items": items,
            "filters": filters,
            "export_profiles": known_profiles("csv"),
        },
    )


@router.get("/rows", response_class=HTMLResponse)
def collection_rows(request: Request, collection: CollectionServiceDep) -> HTMLResponse:
    filters = _filters(request)
    items = collection.list_items(**_service_filters(filters), limit=200)  # type: ignore[arg-type]
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request, "partials/collection_results.html", {"items": items, "filters": filters}
    )


def _row_response(request: Request, item: object) -> HTMLResponse:
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "partials/collection_row.html", {"item": item})


@router.post("/{item_id}/quantity", response_class=HTMLResponse)
def adjust_quantity(
    item_id: int, request: Request, collection: CollectionServiceDep, delta: int = 0
) -> HTMLResponse:
    item = collection.get_item(item_id)
    new_quantity = max(0, item.quantity + delta)
    collection.set_quantity(item_id, new_quantity)
    if new_quantity == 0:
        return HTMLResponse("")  # a linha some da tabela (hx-swap="outerHTML")
    return _row_response(request, collection.get_item(item_id))


@router.post("/{item_id}/set-print", response_class=HTMLResponse)
def set_print(
    item_id: int,
    request: Request,
    collection: CollectionServiceDep,
    card_print_id: Annotated[int, Form()],
) -> HTMLResponse:
    collection.set_print(item_id, card_print_id)
    return _row_response(request, collection.get_item(item_id))


@router.get("/{item_id}/print-options", response_class=HTMLResponse)
def print_options(
    item_id: int, request: Request, collection: CollectionServiceDep, catalog: CatalogServiceDep
) -> HTMLResponse:
    item = collection.get_item(item_id)
    prints = catalog.prints_for_card(item.card_id)
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request, "partials/print_options.html", {"item": item, "prints": prints}
    )


@router.delete("/{item_id}", response_class=HTMLResponse)
def delete_item(item_id: int, collection: CollectionServiceDep) -> HTMLResponse:
    collection.remove(item_id)
    return HTMLResponse("")
