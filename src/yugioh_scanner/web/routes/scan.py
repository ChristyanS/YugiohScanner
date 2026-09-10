"""Telas `/scan` e `/scan/{id}` (plano §10.2)."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from ...config import BUILTIN_OCR_PROVIDERS
from ..deps import ScanServiceDep, SettingsDep
from ..serializers import job_to_dict, result_to_dict

router = APIRouter()


@router.get("/scan", response_class=HTMLResponse)
def scan_page(request: Request, settings: SettingsDep) -> HTMLResponse:
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "scan.html",
        {
            "providers": BUILTIN_OCR_PROVIDERS,
            "default_provider": settings.ocr_provider,
            "max_upload_mb": settings.max_upload_mb,
            "max_upload_files": settings.max_upload_files,
        },
    )


@router.get("/scan/{job_id}", response_class=HTMLResponse)
def scan_detail(job_id: int, request: Request, scans: ScanServiceDep) -> HTMLResponse:
    job = scans.job_detail(job_id)
    results = [result_to_dict(result, with_image=False) for result in scans.job_results(job_id)]
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request, "scan_detail.html", {"job": job_to_dict(job), "results": results}
    )
