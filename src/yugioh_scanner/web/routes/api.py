"""JSON API (plano §9). Mesmos serviços das rotas HTML — nenhuma lógica aqui."""

from __future__ import annotations

import asyncio
import io
import uuid
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, BackgroundTasks, File, Query, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from ...capture.registry import create_backend
from ...config import Settings
from ...db.tables import Card
from ...errors import InvalidScanPathError, TooManyUploadFilesError, UploadTooLargeError
from ...images import ImageError, has_supported_extension, load_image, looks_like_image
from ...images.grid import parse_grid_size
from ...scanner.discovery import resolve_scan_folder
from ...services.catalog_service import resolve_display_language
from ...services.sync_service import SyncService
from ..card_filter_params import parse_card_attribute_filters
from ..deps import (
    CatalogServiceDep,
    ClientDep,
    CollectionServiceDep,
    DatabaseDep,
    DeckServiceDep,
    ExportServiceDep,
    ImageCacheDep,
    ScanRunnerDep,
    ScanServiceDep,
    SessionDep,
    SettingsDep,
    SettingsServiceDep,
)
from ..serializers import (
    card_to_dict,
    deck_to_dict,
    item_to_dict,
    job_to_dict,
    result_to_dict,
    set_to_dict,
)

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


# ---------------------------------------------------------------- preferências


class UiLanguageBody(BaseModel):
    language: str


@router.post("/settings/ui-language")
def set_ui_language(body: UiLanguageBody, settings: SettingsServiceDep) -> dict[str, Any]:
    settings.set_ui_language(body.language)
    return {"ui_language": settings.get_ui_language()}


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
    settings: SettingsServiceDep,
    q: str | None = None,
    set: str | None = None,
    lang: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> list[dict[str, Any]]:
    # `lang` resolvido do mesmo jeito que `/cards`/`/collection` (idioma
    # explícito > preferência salva > derivado da UI) — sem isto, a busca
    # de nome multilíngue (via `CardAttributeFilters`/`lang` em
    # `CardRepository._matching_ids`) ficava travada em inglês para quem
    # chama este endpoint sem passar `lang` (ex.: a busca de "adicionar
    # manualmente" em `/collection`), mesmo com a coleção toda em
    # português (achado real: regressão ao restringir a busca por idioma
    # — ver `CollectionRepository.list_filtered`).
    resolved_lang = resolve_display_language(lang, default=settings.get_default_card_language())
    cards = catalog.search_cards(q, set_prefix=set, lang=resolved_lang, limit=limit, offset=offset)
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


class SetCreateBody(BaseModel):
    set_code: str
    set_name: str


@router.post("/sets")
def create_set(body: SetCreateBody, catalog: CatalogServiceDep) -> dict[str, Any]:
    card_set = catalog.register_set(body.set_code, body.set_name)
    return set_to_dict(card_set)


# --------------------------------------------------------------------- scans


class ScanStartBody(BaseModel):
    folder: str
    provider: str | None = None
    workers: int | None = None
    recursive: bool = False
    auto: bool = True
    reprocess: bool = False
    grid: bool = False
    #: "3x3" — layout explícito, alternativa à detecção automática por
    #: contorno (que não é confiável em fotos reais, ADR 0011). Informar isto
    #: já liga o modo grade sozinho, não precisa marcar `grid` também.
    grid_size: str | None = None
    #: `None` = usa `Settings.auto_requires_print` (ativado por padrão).
    require_set: bool | None = None
    #: `None` = usa `Settings.auto_requires_rarity` (desligado por padrão).
    require_rarity: bool | None = None


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
    grid_size = parse_grid_size(body.grid_size) if body.grid_size else None

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
        grid=body.grid,
        grid_size=grid_size,
        require_set=body.require_set,
        require_rarity=body.require_rarity,
    )
    return {"run_id": run.run_id, "status": "started"}


@router.get("/scans")
def list_scans(scans: ScanServiceDep, limit: int = Query(10, ge=1, le=100)) -> list[dict[str, Any]]:
    return [job_to_dict(job) for job in scans.recent_jobs(limit=limit)]


@router.get("/scans/{job_id}")
def get_scan(job_id: int, scans: ScanServiceDep) -> dict[str, Any]:
    return job_to_dict(scans.job_detail(job_id))


@router.delete("/scans/{job_id}")
def delete_scan(job_id: int, scans: ScanServiceDep) -> dict[str, bool]:
    scans.delete_job(job_id)
    return {"removed": True}


@router.delete("/scans")
def clear_scans(scans: ScanServiceDep) -> dict[str, int]:
    return {"removed": scans.clear_all_jobs()}


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
    #: Set escolhido no seletor em cascata quando `card_print_id` é `None`
    #: (raridade ainda pendente/não catalogada).
    set_code_full: str | None = None
    #: Raridade digitada livremente quando nenhuma das catalogadas bate —
    #: só junto de `set_code_full`, nunca junto de `card_print_id`.
    rarity_override: str | None = None
    quantity: int = Field(default=1, ge=1)
    #: Idioma da carta física escolhido na revisão. Omitido, o serviço cai
    #: para o idioma que o matching detectou sozinho (`ScanResult.detected_language`).
    language: str | None = None


@router.post("/scan-results/{result_id}/confirm")
def confirm_scan_result(result_id: int, body: ConfirmBody, scans: ScanServiceDep) -> dict[str, Any]:
    item = scans.confirm_result(
        result_id,
        card_id=body.card_id,
        card_print_id=body.card_print_id,
        set_code_full=body.set_code_full,
        rarity_override=body.rarity_override,
        quantity=body.quantity,
        language=body.language,
    )
    return item_to_dict(item)


@router.post("/scan-results/{result_id}/reject")
def reject_scan_result(result_id: int, scans: ScanServiceDep) -> dict[str, bool]:
    scans.reject_result(result_id)
    return {"rejected": True}


@router.post("/scan-results/reject-all")
def reject_all_scan_results(scans: ScanServiceDep) -> dict[str, int]:
    return {"removed": scans.reject_all_pending()}


# ---------------------------------------------------------------- coleção


@router.get("/collection")
def list_collection(
    collection: CollectionServiceDep,
    search: str | None = None,
    set: str | None = None,
    no_set: bool = False,
    no_rarity: bool = False,
    sort: str = "name",
    desc: bool = False,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> list[dict[str, Any]]:
    items = collection.list_items(
        search=search,
        set_prefix=set,
        no_set=no_set,
        no_rarity=no_rarity,
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
    #: Desempata `set_code` ambíguo (2+ raridades catalogadas). Se não bater
    #: com nenhuma catalogada, o item entra com o set conhecido e a raridade
    #: como texto pendente em vez de erro.
    rarity: str | None = None
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
        rarity=body.rarity,
        quantity=body.quantity,
        condition=body.condition,
        edition=body.edition,
        language=body.language,
        notes=body.notes,
    )
    return item_to_dict(item)


class CollectionPatchBody(BaseModel):
    """Campos que a tela `/collection` edita inline (plano §10.4): quantidade,
    o set de um item indefinido, e agora a raridade (deixou de fazer parte da
    chave lógica ambígua que motivava a restrição original — set e raridade
    são desacoplados). `condition`/`edition`/`language` continuam de fora:
    exigiriam decidir como fundir com uma linha já existente na chave nova —
    fora do que a tela pede; ver docs/PLAN.md para a nota completa."""

    quantity: int | None = Field(default=None, ge=0)
    set_code: str | None = None
    rarity: str | None = None
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
    if body.rarity is not None:
        collection.resolve_rarity(item_id, body.rarity)
    if body.notes is not None:
        collection.set_notes(item_id, body.notes)
    return item_to_dict(collection.get_item(item_id))


@router.delete("/collection/{item_id}")
def delete_collection_item(item_id: int, collection: CollectionServiceDep) -> dict[str, bool]:
    collection.remove(item_id)
    return {"removed": True}


@router.delete("/collection")
def clear_collection(collection: CollectionServiceDep) -> dict[str, int]:
    return {"removed": collection.clear_all()}


# ------------------------------------------------------------- deck builder


class DeckCreateBody(BaseModel):
    name: str
    build_mode: str = "full_db"
    banlist: str = "TCG"


class DeckPatchBody(BaseModel):
    name: str | None = None
    cover_card_id: int | None = None


class DeckCardBody(BaseModel):
    card_id: int
    zone: str
    quantity: int = Field(default=1, ge=1)


@router.get("/decks")
def list_decks(decks: DeckServiceDep) -> list[dict[str, Any]]:
    # Inclui as cartas de cada deck (mesma assinatura de `get_deck`) para a
    # galeria calcular a capa sem N chamadas extras quando `cover_card_id`
    # não foi definido — mesmo fallback já usado em `deck_editor.html`
    # (`cover_card_id` ou a primeira carta do deck).
    return [
        deck_to_dict(d, cards=decks.repo.cards_for_deck(d.id)) for d in decks.list_decks()
    ]


@router.post("/decks")
def create_deck(body: DeckCreateBody, decks: DeckServiceDep) -> dict[str, Any]:
    deck = decks.create_deck(body.name, build_mode=body.build_mode, banlist=body.banlist)
    return deck_to_dict(deck)


@router.get("/decks/{deck_id}")
def get_deck(deck_id: int, decks: DeckServiceDep) -> dict[str, Any]:
    deck = decks.get_deck(deck_id)
    return deck_to_dict(deck, cards=decks.repo.cards_for_deck(deck_id))


@router.put("/decks/{deck_id}")
def update_deck(deck_id: int, body: DeckPatchBody, decks: DeckServiceDep) -> dict[str, Any]:
    deck = decks.get_deck(deck_id)
    if body.name is not None:
        deck = decks.rename_deck(deck_id, body.name)
    # `model_fields_set` (não só "não é None") distingue "campo omitido" de
    # "cliente mandou null de propósito para limpar a capa do deck".
    if "cover_card_id" in body.model_fields_set:
        deck = decks.set_cover(deck_id, body.cover_card_id)
    return deck_to_dict(deck)


@router.delete("/decks/{deck_id}")
def delete_deck(deck_id: int, decks: DeckServiceDep) -> dict[str, bool]:
    decks.delete_deck(deck_id)
    return {"removed": True}


@router.delete("/decks")
def clear_decks(decks: DeckServiceDep) -> dict[str, int]:
    return {"removed": decks.clear_all_decks()}


@router.get("/decks/{deck_id}/search")
def search_deck_pool(
    deck_id: int,
    request: Request,
    decks: DeckServiceDep,
    settings: SettingsServiceDep,
    q: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> list[dict[str, Any]]:
    deck = decks.get_deck(deck_id)
    filters = parse_card_attribute_filters(request.query_params)
    lang = resolve_display_language(
        request.query_params.get("lang"), default=settings.get_default_card_language()
    )
    cards = decks.searchable_pool(deck, q, filters=filters, lang=lang, limit=limit, offset=offset)
    payload = [card_to_dict(c) for c in cards]
    if deck.build_mode == "collection_only":
        # Selo "cópias restantes" (plano do Deck Builder §5) — só faz sentido
        # neste modo, onde a coleção é o teto de quantas cópias entram.
        for card_dict, card in zip(payload, cards, strict=True):
            card_dict["remaining"] = decks.remaining_copies(deck, card)
    return payload


@router.post("/decks/{deck_id}/cards")
def add_deck_card(deck_id: int, body: DeckCardBody, decks: DeckServiceDep) -> dict[str, Any]:
    decks.add_card(deck_id, body.card_id, body.zone, quantity=body.quantity)
    deck = decks.get_deck(deck_id)
    return deck_to_dict(deck, cards=decks.repo.cards_for_deck(deck_id))


@router.delete("/decks/{deck_id}/cards/{card_id}")
def remove_deck_card(
    deck_id: int,
    card_id: int,
    decks: DeckServiceDep,
    zone: str,
    quantity: int | None = None,
) -> dict[str, Any]:
    decks.remove_card(deck_id, card_id, zone, quantity=quantity)
    deck = decks.get_deck(deck_id)
    return deck_to_dict(deck, cards=decks.repo.cards_for_deck(deck_id))


@router.get("/decks/{deck_id}/validate")
def validate_deck(deck_id: int, decks: DeckServiceDep) -> dict[str, Any]:
    result = decks.validate_deck(deck_id)
    return {
        "legal": result.legal,
        "issues": result.issues,
        "main_count": result.main_count,
        "extra_count": result.extra_count,
        "side_count": result.side_count,
    }


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


# ------------------------------------------------------------------ captura


@router.get("/capture/devices")
def list_capture_devices() -> list[dict[str, Any]]:
    """Dispositivos WIA disponíveis (plano §22, ADR 0011).

    Pode levantar `CaptureError` (não é Windows, `pywin32` ausente, nenhum
    dispositivo instalado) — o handler global já converte isso em JSON com
    `hint`, mesmo mecanismo de qualquer outro `YugiohScannerError` da API.
    """
    backend = create_backend("wia")
    return [{"id": device.id, "name": device.name} for device in backend.list_devices()]


class CaptureStartBody(BaseModel):
    device_id: str | None = None
    dpi: int = 300
    color: bool = True
    #: Pasta devolvida por uma chamada anterior desta mesma sequência de
    #: páginas — omitido, começa uma pasta nova. Existe porque capturar em
    #: lote (várias páginas numa chamada só) não dá tempo real de trocar a
    #: folha na mesa do scanner entre uma captura e outra (achado real de
    #: uso, plano §22): agora é uma página por chamada, e o cliente reenvia
    #: o `folder` da resposta anterior para acumular todas no mesmo lugar.
    folder: str | None = None


def _resolve_capture_folder(raw: str | None, settings: Settings) -> Path:
    if raw is None:
        return settings.uploads_path / "captures" / uuid.uuid4().hex
    base = (settings.uploads_path / "captures").resolve()
    candidate = Path(raw).resolve()
    if candidate != base and base not in candidate.parents:
        raise InvalidScanPathError(f"Pasta de captura inválida: {raw!r}.")
    return candidate


@router.post("/capture")
def start_capture(body: CaptureStartBody, settings: SettingsDep) -> dict[str, Any]:
    """Captura **uma** página por chamada — mesmo formato de resposta de
    `POST /uploads` ({folder, ...}), para o JS de `/scan` reusar o mesmo
    fluxo "pega folder, preenche o campo, o form dispara o scan". O cliente
    chama este endpoint uma vez por página física, dando tempo real de
    trocar a folha entre uma chamada e outra."""
    backend = create_backend("wia")
    capture_dir = _resolve_capture_folder(body.folder, settings)
    backend.capture(
        capture_dir,
        device_id=body.device_id,
        dpi=body.dpi,
        color_mode="color" if body.color else "gray",
    )
    return {"folder": str(capture_dir)}


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
