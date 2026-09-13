"""Camada de banco de dados: schema, engine, sessão e índice de busca."""

from .engine import (
    check_foreign_keys,
    check_integrity,
    create_db_engine,
    database_exists,
    database_size_bytes,
    engine_from_settings,
    vacuum,
)
from .fts import rebuild_fts, sanitize_fts_query, search_card_ids
from .session import Database, session_scope
from .tables import (
    Base,
    Card,
    CardImage,
    CardPrint,
    CardPrintOverride,
    CardSet,
    CollectionItem,
    ScanImage,
    ScanJob,
    ScanResult,
    SyncState,
    utcnow,
)

__all__ = [
    "Base",
    "Card",
    "CardImage",
    "CardPrint",
    "CardPrintOverride",
    "CardSet",
    "CollectionItem",
    "Database",
    "ScanImage",
    "ScanJob",
    "ScanResult",
    "SyncState",
    "check_foreign_keys",
    "check_integrity",
    "create_db_engine",
    "database_exists",
    "database_size_bytes",
    "engine_from_settings",
    "rebuild_fts",
    "sanitize_fts_query",
    "search_card_ids",
    "session_scope",
    "utcnow",
    "vacuum",
]
