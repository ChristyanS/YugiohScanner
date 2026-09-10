"""Acesso a dados: traduz domínio ↔ SQL, sem regra de negócio."""

from .cards import CardRepository
from .collection import CollectionKey, CollectionRepository
from .scans import ScanRepository
from .sets import SetRepository
from .sync_state import SyncStateRepository

__all__ = [
    "CardRepository",
    "CollectionKey",
    "CollectionRepository",
    "ScanRepository",
    "SetRepository",
    "SyncStateRepository",
]
