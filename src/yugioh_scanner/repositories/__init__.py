"""Acesso a dados: traduz domínio ↔ SQL, sem regra de negócio."""

from .collection import CollectionKey, CollectionRepository
from .scans import ScanRepository
from .sync_state import SyncStateRepository

__all__ = [
    "CollectionKey",
    "CollectionRepository",
    "ScanRepository",
    "SyncStateRepository",
]
