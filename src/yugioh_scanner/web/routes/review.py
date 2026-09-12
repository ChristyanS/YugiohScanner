"""Tela `/review` (plano §10.3) — a mais importante do fluxo.

A página em si só monta o esqueleto; a fila de pendências, os candidatos e as
ações (confirmar/rejeitar) são todos buscados/disparados pelo JS embutido no
template direto contra `/api/v1/scan-results/*` — é a mesma API que a CLI e
qualquer outro cliente usariam, sem rota HTML paralela duplicando lógica.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from ...db.tables import ALT_NAME_LANGUAGES
from ..deps import ScanServiceDep

router = APIRouter()

#: Idioma da carta física é texto livre no banco (`collection_item.language`
#: não tem CHECK), mas o `<select>` da revisão precisa de uma lista finita —
#: a mesma que o app sincroniza/exibe em outras telas (db/tables.py).
_LANGUAGE_OPTIONS = ("EN", *ALT_NAME_LANGUAGES)


@router.get("/review", response_class=HTMLResponse)
def review_page(request: Request, scans: ScanServiceDep) -> HTMLResponse:
    pending_count = len(scans.pending_results(limit=10_000))
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "review.html",
        {"pending_count": pending_count, "languages": _LANGUAGE_OPTIONS},
    )
