"""Serviços de aplicação — os casos de uso.

É a API interna que CLI e Web consomem. Nenhuma regra de negócio mora nas
interfaces; nenhum serviço conhece HTTP ou terminal.
"""

from .collection_service import CollectionService, CollectionStats
from .scan_service import ImageEvent, ScanRunReport, ScanService
from .sync_service import SyncDecision, SyncReport, SyncService

__all__ = [
    "CollectionService",
    "CollectionStats",
    "ImageEvent",
    "ScanRunReport",
    "ScanService",
    "SyncDecision",
    "SyncReport",
    "SyncService",
]
