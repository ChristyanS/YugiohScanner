"""JSON API (plano §9). Mesmos serviços das rotas HTML — nenhuma lógica aqui."""

from __future__ import annotations

import asyncio
import io
import uuid
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, BackgroundTasks, File, Query, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from ...db.tables import Card
from ...errors import TooManyUploadFilesError, UploadTooLargeError
from ...images import ImageError, has_supported_extension, load_image, looks_like_image
from ...scanner.discovery import resolve_scan_folder
from ...services.sync_service import SyncService
from ..deps import (
    CatalogServiceDep,
    ClientDep,
    CollectionServiceDep,
    DatabaseDep,
    ExportServiceDep,
    ImageCacheDep,
    ScanRunnerDep,
    ScanServiceDep,
    SessionDep,
    SettingsDep,
)
from ..serializers import card_to_dict, item_to_dict, job_to_dict, result_to_dict, set_to_dict

router = APIRouter()


# --------------------------------------------------------------------- saúde


@router.get("/health")
def health(session: SessionDep, settings: SettingsDep) -> dict[str, Any]:
    cards = session.scalar(select(func.count()).select_from(Card)) or 0
    return {
        "status": "ok",
        "database_path": str(settings.database_path),
        "catalog_cards": cards,
    }


@router.get("/stats")
def stats(collection: CollectionServiceDep, scans: ScanServiceDep) -> dict[str, Any]:
    result = collection.stats().as_dict()
    result["pending_review"] = len(scans.pending_results(limit=10_000))
    return result


# ---------------------------------------------------------------------- sync


@router.post("/sync")
def start_sync(
    background_tasks: BackgroundTasks,
    database: DatabaseDep,
    client: ClientDep,
    settings: SettingsDep,
) -> dict[str, str]:
    service = SyncService(database, client, settings)
    background_tasks.add_task(service.sync)
    return {"status": "started"}


# --------------------------------------------------------------------- cartas


@router.get("/cards")
def search_cards(
    catalog: CatalogServiceDep,
    q: str | None = None,
    set: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> list[dict[str, Any]]:
    cards = catalog.search_cards(q, set_prefix=set, limit=limit, offset=offset)
    return [card_to_dict(card) for card in cards]


@router.get("/cards/{card_id}")
def get_card(
    card_id: int, catalog: CatalogServiceDep, collection: CollectionServiceDep
) -> dict[str, Any]:
    card = catalog.get_card(card_id)
    payload = card_to_dict(card, with_prints=True)
    owned = collection.repo.list_for_card(card_id)
    payload["owned"] = [item_to_dict(item) for item in owned]
    return payload


@router.get("/cards/{card_id}/image")
def get_card_image(
    card_id: int,
    catalog: CatalogServiceDep,
    cache: ImageCacheDep,
    size: Literal["small", "full"] = "small",
) -> FileResponse:
    card = catalog.get_card(card_id)
    path = cache.ensure_cached(card, size)
    return FileResponse(
        path,
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


@router.get("/sets")
def list_sets(
    catalog: CatalogServiceDep,
    limit: int = Query(1000, ge=1, le=1000),
    offset: int = 0,
) -> list[dict[str, Any]]:
    return [set_to_dict(s) for s in catalog.list_sets(limit=limit, offset=offset)]


# --------------------------------------------------------------------- scans


class ScanStartBody(BaseModel):
    folder: str
    provider: str | None = None
    workers: int | None = None
    recursive: bool = False
    auto: bool = True
    reprocess: bool = False


@router.post("/scans")
async def start_scan(
    body: ScanStartBody,
    settings: SettingsDep,
    database: DatabaseDep,
    runner: ScanRunnerDep,
) -> dict[str, str]:
    # Validado aqui (não só dentro da thread) para que um caminho ruim vire
    # erro HTTP imediato, em vez de aparecer só no primeiro evento do SSE.
    resolve_scan_folder(body.folder, settings)

    loop = asyncio.get_running_loop()
    run = runner.start(
        database,
        settings,
        loop,
        folder=body.folder,
        provider_name=body.provider,
        workers=body.workers,
        recursive=body.recursive,
        no_auto=not body.auto,
        reprocess=body.reprocess,
    )
    return {"run_id": run.run_id, "status": "started"}


@router.get("/scans")
def list_scans(scans: ScanServiceDep, limit: int = Query(10, ge=1, le=100)) -> list[dict[str, Any]]:
    return [job_to_dict(job) for job in scans.recent_jobs(limit=limit)]


@router.get("/scans/{job_id}")
def get_scan(job_id: int, scans: ScanServiceDep) -> dict[str, Any]:
    return job_to_dict(scans.job_detail(job_id))


@router.get("/scans/{job_id}/results")
def get_scan_results(
    job_id: int, scans: ScanServiceDep, decision: str | None = None
) -> list[dict[str, Any]]:
    results = scans.job_results(job_id)
    if decision:
        results = [r for r in results if r.decision == decision]
    return [result_to_dict(r) for r in results]


@router.get("/scans/live/{run_id}/events")
async def scan_events(run_id: str, runner: ScanRunnerDep) -> StreamingResponse:
    run = runner.get(run_id)
    if run is None:

        async def missing() -> Any:
            # Nome de evento SSE deliberadamente diferente de "error": esse é
            # reservado pelo `EventSource` do navegador para erro de CONEXÃO
            # (dispatch automático, sem `data`) — reusar o nome misturaria os
            # dois casos no mesmo listener do lado do cliente.
            yield 'event: scan-error\ndata: {"message": "run desconhecido ou expirado"}\n\n'

        return StreamingResponse(missing(), media_type="text/event-stream")

    async def stream() -> Any:
        while True:
            item = await run.queue.get()
            if item is None:
                break
            event_name = "scan-error" if item["type"] == "error" else item["type"]
            yield f"event: {event_name}\ndata: {_json(item)}\n\n"

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.delete("/scans/live/{run_id}")
def cancel_scan(run_id: str, runner: ScanRunnerDep) -> dict[str, bool]:
    return {"cancelled": runner.cancel(run_id)}


@router.get("/scan-images/{image_id}/file")
def get_scan_image_file(image_id: int, scans: ScanServiceDep) -> FileResponse:
    """A foto original ao lado da leitura, na tela de revisão (plano §10.3).

    Só aceita o ID inteiro — o caminho de verdade é resolvido no servidor a
    partir do que o próprio scan gravou, nunca de um caminho vindo do cliente
    (mesmo princípio de `/cards/{id}/image`, plano §21).
    """
    path = scans.get_image_path(image_id)
    return FileResponse(path)


# ------------------------------------------------------------- scan-results


@router.get("/scan-results")
def pending_scan_results(
    scans: ScanServiceDep, limit: int = Query(100, ge=1, le=500)
) -> list[dict[str, Any]]:
    return [result_to_dict(r) for r in scans.pending_results(limit=limit)]


@router.get("/scan-results/{result_id}")
def get_scan_result(result_id: int, scans: ScanServiceDep) -> dict[str, Any]:
    return result_to_dict(scans.get_result(result_id))


class ConfirmBody(BaseModel):
    card_id: int
    card_print_id: int | None = None
    quantity: int = Field(default=1, ge=1)


@router.post("/scan-results/{result_id}/confirm")
def confirm_scan_result(result_id: int, body: ConfirmBody, scans: ScanServiceDep) -> dict[str, Any]:
    item = scans.confirm_result(
        result_id,
        card_id=body.card_id,
        card_print_id=body.card_print_id,
        quantity=body.quantity,
    )
    return item_to_dict(item)


@router.post("/scan-results/{result_id}/reject")
def reject_scan_result(result_id: int, scans: ScanServiceDep) -> dict[str, bool]:
    scans.reject_result(result_id)
    return {"rejected": True}


# ---------------------------------------------------------------- coleção


@router.get("/collection")
def list_collection(
    collection: CollectionServiceDep,
    search: str | None = None,
    set: str | None = None,
    no_set: bool = False,
    sort: str = "name",
    desc: bool = False,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> list[dict[str, Any]]:
    items = collection.list_items(
        search=search,
        set_prefix=set,
        no_set=no_set,
        sort=sort,
        descending=desc,
        limit=limit,
        offset=offset,
    )
    return [item_to_dict(item) for item in items]


class CollectionAddBody(BaseModel):
    name: str
    set_code: str | None = None
    #: Alternativa a `set_code`: usada pela tela `/collection` (Fase 3), que já
    #: sabe o `id` exato do print escolhido (veio de `/api/v1/cards/{id}`) e
    #: não precisa reabrir a resolução por código.
    card_print_id: int | None = None
    quantity: int = Field(default=1, ge=1)
    condition: str = "Near Mint"
    edition: str = "Unlimited"
    language: str = "EN"
    notes: str | None = None


@router.post("/collection")
def add_collection_item(
    body: CollectionAddBody, collection: CollectionServiceDep
) -> dict[str, Any]:
    item = collection.add_manual(
        body.name,
        set_code=body.set_code,
        card_print_id=body.card_print_id,
        quantity=body.quantity,
        condition=body.condition,
        edition=body.edition,
        language=body.language,
        notes=body.notes,
    )
    return item_to_dict(item)


class CollectionPatchBody(BaseModel):
    """Só os campos que a tela `/collection` de fato edita inline (plano §10.4):
    quantidade e o set de um item indefinido. `condition`/`edition`/`language`
    exigiriam decidir como fundir com uma linha já existente na chave nova —
    fora do que a tela pede; ver docs/PLAN.md para a nota completa."""

    quantity: int | None = Field(default=None, ge=0)
    set_code: str | None = None
    notes: str | None = None


@router.patch("/collection/{item_id}")
def patch_collection_item(
    item_id: int, body: CollectionPatchBody, collection: CollectionServiceDep
) -> dict[str, Any]:
    if body.quantity is not None:
        collection.set_quantity(item_id, body.quantity)
        if body.quantity == 0:
            return {"id": item_id, "removed": True}
    if body.set_code is not None:
        collection.resolve_print_by_code(item_id, body.set_code)
    if body.notes is not None:
        collection.set_notes(item_id, body.notes)
    return item_to_dict(collection.get_item(item_id))


@router.delete("/collection/{item_id}")
def delete_collection_item(item_id: int, collection: CollectionServiceDep) -> dict[str, bool]:
    collection.remove(item_id)
    return {"removed": True}


# --------------------------------------------------------------- exportação


@router.get("/export")
def export_collection(
    export: ExportServiceDep,
    format: Literal["csv", "txt"] = "csv",
    profile: str | None = None,
    skip_unresolved: bool = False,
) -> StreamingResponse:
    text, resolved = export.export_to_string(
        fmt=format, profile_name=profile, skip_unresolved=skip_unresolved
    )
    data = text.encode(resolved.encoding)
    filename = f"collection-{resolved.name}.{format}"
    return StreamingResponse(
        io.BytesIO(data),
        media_type="text/csv" if format == "csv" else "text/plain",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ------------------------------------------------------------------ uploads


@router.post("/uploads")
async def upload_images(
    settings: SettingsDep,
    files: Annotated[list[UploadFile], File()],
) -> dict[str, Any]:
    """Recebe fotos do navegador (plano §14.1, §21): nome descartado,
    extensão + magic bytes + decompression bomb validados, gravadas em
    `data/uploads/<lote>/` — a mesma pasta vira `folder` de `POST /scans`."""
    if len(files) > settings.max_upload_files:
        raise TooManyUploadFilesError(len(files), settings.max_upload_files)

    batch_dir = settings.uploads_path / uuid.uuid4().hex
    batch_dir.mkdir(parents=True, exist_ok=True)
    max_bytes = settings.max_upload_mb * 1024 * 1024

    saved = 0
    rejected: list[str] = []
    for upload in files:
        ext = _safe_extension(upload.filename)
        destination = batch_dir / f"{uuid.uuid4().hex}{ext}"
        size = 0
        with destination.open("wb") as handle:
            while chunk := await upload.read(1024 * 1024):
                size += len(chunk)
                if size > max_bytes:
                    handle.close()
                    destination.unlink(missing_ok=True)
                    raise UploadTooLargeError(settings.max_upload_mb)
                handle.write(chunk)

        if not has_supported_extension(destination) or not looks_like_image(destination):
            destination.unlink(missing_ok=True)
            rejected.append(upload.filename or "(sem nome)")
            continue
        try:
            load_image(destination, max_pixels=settings.max_image_pixels).close()
        except ImageError:
            destination.unlink(missing_ok=True)
            rejected.append(upload.filename or "(sem nome)")
            continue
        saved += 1

    return {"folder": str(batch_dir), "saved": saved, "rejected": rejected}


_ALLOWED_UPLOAD_SUFFIXES = {"jpg", "jpeg", "png"}


def _safe_extension(filename: str | None) -> str:
    """Só a extensão sobrevive do nome original — nunca o caminho (plano §21).

    O conteúdo é validado de verdade depois (magic bytes + `Image.verify()`);
    isto só decide o sufixo do nome gerado, então um valor não reconhecido
    cai em `.jpg` sem risco — o arquivo é rejeitado adiante se não for imagem.
    """
    suffix = Path(filename or "").suffix.lstrip(".").lower()
    return f".{suffix}" if suffix in _ALLOWED_UPLOAD_SUFFIXES else ".jpg"


def _json(payload: dict[str, Any]) -> str:
    import json

    return json.dumps(payload, default=str, ensure_ascii=False)
