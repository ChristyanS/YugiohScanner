"""Tela `/` — dashboard (plano §10.1)."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from ..deps import CollectionServiceDep, ScanServiceDep
from ..serializers import job_to_dict

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request, collection: CollectionServiceDep, scans: ScanServiceDep
) -> HTMLResponse:
    stats = collection.stats()
    pending = scans.pending_results(limit=10_000)
    jobs = scans.recent_jobs(limit=5)
    recent_items = collection.list_items(sort="added", descending=True, limit=8)

    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "stats": stats,
            "pending_count": len(pending),
            "jobs": [job_to_dict(job) for job in jobs],
            "recent_items": recent_items,
        },
    )
