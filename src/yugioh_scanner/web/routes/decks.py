"""Telas `/decks` (lista) e `/decks/{id}` (editor) — Deck Builder.

Mesmo padrão de `review.py`/`scan.py`: a página só monta o esqueleto, tudo
o resto (listar decks, buscar cartas, adicionar/remover, validar) é
JS embutido contra `/api/v1/decks/*` — a mesma API que qualquer outro
cliente usaria, sem lógica duplicada aqui.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from ...db.tables import DECK_BANLISTS, DECK_BUILD_MODES
from ..deps import CatalogServiceDep

router = APIRouter()


@router.get("/decks", response_class=HTMLResponse)
def decks_page(request: Request) -> HTMLResponse:
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "decks.html",
        {"build_modes": DECK_BUILD_MODES, "banlists": DECK_BANLISTS},
    )


@router.get("/decks/{deck_id}", response_class=HTMLResponse)
def deck_editor_page(deck_id: int, request: Request, catalog: CatalogServiceDep) -> HTMLResponse:
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "deck_editor.html",
        # Opções dos `<select>` de Tipo/Atributo/Race do painel de filtros
        # avançados da busca (mesmo painel de `/cards`/`/collection`).
        {"deck_id": deck_id, "filter_options": catalog.filter_options()},
    )
