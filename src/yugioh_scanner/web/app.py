"""Criação da app FastAPI (plano §9-10, Fase 8).

Duas superfícies, uma única app: rotas HTML (HTMX) em `routes/*` e JSON em
`routes/api.py`, ambas chamando os **mesmos serviços** — nenhuma lógica de
negócio duplicada entre elas (plano §9).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import __version__
from ..config import Settings, get_settings
from ..db.engine import engine_from_settings
from ..db.session import Database
from ..errors import (
    AmbiguousCardError,
    InvalidScanPathError,
    PrintNotFoundForCardError,
    ScanResultAlreadyAppliedError,
    UnknownExportProfileError,
    UploadError,
    YugiohScannerError,
)
from ..images.cache import ImageCache
from ..logging_setup import get_logger
from ..ygoprodeck.client import YgoProDeckClient
from .scan_runner import ScanRunnerRegistry

log = get_logger(__name__)

_WEB_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = _WEB_DIR / "templates"
STATIC_DIR = _WEB_DIR / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.globals["app_version"] = __version__


def _error_status(exc: YugiohScannerError) -> int:
    """Mapeia a hierarquia de `errors.py` para status HTTP (plano §16 aplicado à Web).

    A CLI usa `exit_code` (0/1/2); a Web precisa de granularidade que um exit
    code binário não tem — "não encontrado" e "conflito" merecem status
    diferentes mesmo vindo da mesma família de erro. `*NotFoundError` é
    verificado pelo nome (não por `isinstance`) de propósito: é uma convenção
    seguida por toda a hierarquia (`CardNotFoundError`, `JobNotFoundError`,
    ...) e listar cada uma aqui seria só repetir `errors.py`.
    """
    if type(exc).__name__.endswith("NotFoundError"):
        return 404
    if isinstance(
        exc, AmbiguousCardError | PrintNotFoundForCardError | ScanResultAlreadyAppliedError
    ):
        return 409
    if isinstance(exc, InvalidScanPathError | UnknownExportProfileError | UploadError):
        return 422
    return 500


async def _handle_domain_error(request: Request, exc: Exception) -> JSONResponse | HTMLResponse:
    assert isinstance(exc, YugiohScannerError)
    status = _error_status(exc)
    is_api = request.url.path.startswith("/api/")
    is_htmx = request.headers.get("hx-request") == "true"

    if is_api or not is_htmx:
        # JSON para a API; página cheia (não-HTMX) também recebe JSON aqui
        # porque toda navegação HTML desta app passa por HTMX — um GET direto
        # de página cheia que falha já é caso raro o bastante para não
        # justificar uma segunda template de erro completa.
        return JSONResponse({"error": exc.user_message, "hint": exc.hint}, status_code=status)

    # Fragmento HTMX: um `<div>` de erro que o cliente sabe onde encaixar
    # (todo formulário desta app tem um alvo `#form-error` ao lado do botão).
    html = templates.get_template("partials/error.html").render(
        {"request": request, "message": exc.user_message, "hint": exc.hint}
    )
    return HTMLResponse(content=html, status_code=status)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Fábrica da app — testável: `create_app(settings_de_teste)` isola cada suíte."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        resolved = settings or get_settings()
        engine = engine_from_settings(resolved)
        database = Database(engine)
        client = YgoProDeckClient(resolved)

        app.state.settings = resolved
        app.state.database = database
        app.state.client = client
        app.state.image_cache = ImageCache(resolved, client)
        app.state.scan_runner = ScanRunnerRegistry()

        log.info("web.startup", host=resolved.web_host, port=resolved.web_port)
        try:
            yield
        finally:
            client.close()
            database.dispose()

    app = FastAPI(title="Yu-Gi-Oh! Collection Scanner", lifespan=lifespan)
    app.state.templates = templates
    app.add_exception_handler(YugiohScannerError, _handle_domain_error)

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    from .routes import api, cards, collection, dashboard, review, scan

    app.include_router(dashboard.router)
    app.include_router(scan.router)
    app.include_router(review.router)
    app.include_router(collection.router)
    app.include_router(cards.router)
    app.include_router(api.router, prefix="/api/v1")

    return app
