"""Criação da app FastAPI (plano §9-10, Fase 8).

Duas superfícies, uma única app: rotas HTML (HTMX) em `routes/*` e JSON em
`routes/api.py`, ambas chamando os **mesmos serviços** — nenhuma lógica de
negócio duplicada entre elas (plano §9).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import jinja2
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import __version__
from ..config import Settings, get_settings
from ..db.engine import engine_from_settings
from ..db.session import Database
from ..db.tables import DEFAULT_UI_LANGUAGE, UI_LANGUAGES
from ..errors import (
    AmbiguousCardError,
    CaptureError,
    DeckValidationError,
    InvalidScanPathError,
    InvalidSetDataError,
    InvalidSettingValueError,
    PrintNotFoundForCardError,
    ScannerUnavailableError,
    ScannerUnsupportedPlatformError,
    ScanResultAlreadyAppliedError,
    SetAlreadyExistsError,
    UnknownExportProfileError,
    UploadError,
    YugiohScannerError,
)
from ..images.cache import ImageCache
from ..images.grid import InvalidGridSizeError
from ..logging_setup import get_logger
from ..services.settings_service import SettingsService
from ..ygoprodeck.client import YgoProDeckClient
from .i18n import catalog_json, translate
from .scan_runner import ScanRunnerRegistry

log = get_logger(__name__)

_WEB_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = _WEB_DIR / "templates"
STATIC_DIR = _WEB_DIR / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.globals["app_version"] = __version__


@jinja2.pass_context
def _t(context: jinja2.runtime.Context, key: str, **kwargs: object) -> str:
    """Texto traduzido no idioma da request atual (plano de idioma global).

    `request.state.ui_language` é preenchido pelo middleware
    `_resolve_ui_language` abaixo, antes de qualquer rota renderizar
    template — `Jinja2Templates` sempre injeta `request` no contexto.

    `**kwargs` faz substituição simples de `{placeholder}` (mesmo algoritmo
    do helper `t()` em JS, ver `base.html`) — o bastante para os poucos
    textos parametrizados (contagens, nomes) sem precisar de uma lib de
    i18n de verdade para só 2 idiomas.
    """
    request: Request = context["request"]
    lang = getattr(request.state, "ui_language", DEFAULT_UI_LANGUAGE)
    text = translate(lang, key)
    for param_key, value in kwargs.items():
        text = text.replace("{" + param_key + "}", str(value))
    return text


@jinja2.pass_context
def _t_catalog_json(context: jinja2.runtime.Context) -> str:
    """Catálogo completo do idioma atual, em JSON — consumido por
    `window.I18N`/`t()` em `base.html` para strings dentro de `<script>`
    inline, que o global `t()` do Jinja não alcança."""
    request: Request = context["request"]
    lang = getattr(request.state, "ui_language", DEFAULT_UI_LANGUAGE)
    return catalog_json(lang)


templates.env.globals["t"] = _t
templates.env.globals["t_catalog_json"] = _t_catalog_json


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
        exc,
        AmbiguousCardError
        | PrintNotFoundForCardError
        | ScanResultAlreadyAppliedError
        | SetAlreadyExistsError,
    ):
        return 409
    if isinstance(exc, ScannerUnsupportedPlatformError | ScannerUnavailableError):
        # Checado **antes** do `CaptureError` genérico (ambas são subclasses
        # dele): "este ambiente não tem/suporta WIA" é estruturalmente
        # diferente de "o backend de captura existe e quebrou em runtime"
        # (`ScanCaptureFailedError`, `NoScannerDeviceFoundError`, ...). 501
        # deixa o cliente (`scan.html`) distinguir "escondo em silêncio,
        # ambiente não suporta" de "mostro o erro, algo quebrou de verdade"
        # sem precisar inspecionar o texto da mensagem (achado real: a seção
        # de captura "sumia da tela" em qualquer falha, inclusive quando o
        # scanner existe e só a listagem de dispositivos deu erro).
        return 501
    if isinstance(
        exc,
        InvalidScanPathError
        | UnknownExportProfileError
        | UploadError
        | CaptureError
        | InvalidGridSizeError
        | InvalidSetDataError
        | InvalidSettingValueError
        | DeckValidationError,
    ):
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

    @app.middleware("http")
    async def _resolve_ui_language(request: Request, call_next):
        """Resolve `request.state.ui_language` antes de qualquer rota rodar,
        para `t()` (Jinja global acima) sempre ter um idioma disponível.

        `?ui_lang=` sobrepõe a preferência salva (mesmo padrão de override de
        `resolve_display_language`) sem persistir nada — só afeta esta
        request. Pula `/static/`: não há texto para traduzir lá, e abrir uma
        sessão de banco por asset seria desperdício. Qualquer falha (banco
        ainda não migrado, etc.) cai no default em vez de derrubar a request
        inteira — resolver o idioma da UI nunca pode ser o motivo de uma
        página quebrar.
        """
        if request.url.path.startswith("/static/"):
            return await call_next(request)
        override = request.query_params.get("ui_lang")
        if override in UI_LANGUAGES:
            request.state.ui_language = override
        else:
            try:
                with request.app.state.database.session() as session:
                    request.state.ui_language = SettingsService(session).get_ui_language()
            except Exception:
                request.state.ui_language = DEFAULT_UI_LANGUAGE
        return await call_next(request)

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    from .routes import api, cards, collection, dashboard, decks, review, scan

    app.include_router(dashboard.router)
    app.include_router(scan.router)
    app.include_router(review.router)
    app.include_router(collection.router)
    app.include_router(cards.router)
    app.include_router(decks.router)
    app.include_router(api.router, prefix="/api/v1")

    return app
