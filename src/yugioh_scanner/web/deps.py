"""Dependências FastAPI (plano §2.3 aplicado à Web).

Engine, cliente HTTP, cache de imagens e o registro de scans em segundo plano
são criados **uma vez** no `lifespan` da app (`web/app.py`) e vivem em
`app.state` — um por processo. Sessão de banco é **por requisição**: cada
dependência abre e fecha a sua, com commit no sucesso e rollback em exceção
(mesmo contrato de `db/session.py::session_scope`, usado pela CLI).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from ..config import Settings
from ..db.session import Database
from ..images.cache import ImageCache
from ..services.catalog_service import CatalogService
from ..services.collection_service import CollectionService
from ..services.export_service import ExportService
from ..services.scan_service import ScanService
from ..ygoprodeck.client import YgoProDeckClient
from .scan_runner import ScanRunnerRegistry


def get_settings_dep(request: Request) -> Settings:
    return request.app.state.settings


def get_database(request: Request) -> Database:
    return request.app.state.database


def get_client(request: Request) -> YgoProDeckClient:
    return request.app.state.client


def get_image_cache(request: Request) -> ImageCache:
    return request.app.state.image_cache


def get_scan_runner(request: Request) -> ScanRunnerRegistry:
    return request.app.state.scan_runner


SettingsDep = Annotated[Settings, Depends(get_settings_dep)]
DatabaseDep = Annotated[Database, Depends(get_database)]
ClientDep = Annotated[YgoProDeckClient, Depends(get_client)]
ImageCacheDep = Annotated[ImageCache, Depends(get_image_cache)]
ScanRunnerDep = Annotated[ScanRunnerRegistry, Depends(get_scan_runner)]


def get_session(database: DatabaseDep) -> Iterator[Session]:
    with database.session() as session:
        yield session


SessionDep = Annotated[Session, Depends(get_session)]


def get_collection_service(session: SessionDep) -> CollectionService:
    return CollectionService(session)


def get_export_service(session: SessionDep) -> ExportService:
    return ExportService(session)


def get_scan_service(database: DatabaseDep, settings: SettingsDep) -> ScanService:
    return ScanService(database, settings)


def get_catalog_service(session: SessionDep) -> CatalogService:
    return CatalogService(session)


CollectionServiceDep = Annotated[CollectionService, Depends(get_collection_service)]
ExportServiceDep = Annotated[ExportService, Depends(get_export_service)]
ScanServiceDep = Annotated[ScanService, Depends(get_scan_service)]
CatalogServiceDep = Annotated[CatalogService, Depends(get_catalog_service)]
