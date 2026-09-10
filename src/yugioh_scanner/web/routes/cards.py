"""Tela `/cards/{id}` (plano §10.5)."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from ..deps import CatalogServiceDep, CollectionServiceDep

router = APIRouter()


@router.get("/cards/{card_id}", response_class=HTMLResponse)
def card_detail(
    card_id: int, request: Request, catalog: CatalogServiceDep, collection: CollectionServiceDep
) -> HTMLResponse:
    card = catalog.get_card(card_id)
    prints = catalog.prints_for_card(card_id)
    owned = collection.repo.list_for_card(card_id)
    owned_print_ids = {item.card_print_id for item in owned if item.card_print_id is not None}
    total_owned = sum(item.quantity for item in owned)

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
        },
    )
