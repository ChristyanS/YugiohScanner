"""Serviços de aplicação — os casos de uso.

É a API interna que CLI e Web consomem. Nenhuma regra de negócio mora nas
interfaces; nenhum serviço conhece HTTP ou terminal.
"""

from .sync_service import SyncDecision, SyncReport, SyncService

__all__ = ["SyncDecision", "SyncReport", "SyncService"]
