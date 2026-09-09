"""Sincronização do catálogo local com a YGOPRODeck (Fase 2).

Regras que dão forma a este serviço:

* **Não depender da API no dia a dia.** Depois do primeiro `init`, tudo roda
  offline; só um `sync` explícito toca a rede.
* **Não re-baixar à toa.** `checkDBVer.php` diz a versão do catálogo deles; se
  não mudou, o sync é um no-op de dois segundos.
* **Tudo ou nada.** A importação inteira acontece em **uma transação**: uma
  falha no meio deixa o catálogo antigo intacto e utilizável (plano §16).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from ..config import Settings
from ..db.session import Database
from ..db.tables import (
    SYNC_KEY_CARD_COUNT,
    SYNC_KEY_DATABASE_DATE,
    SYNC_KEY_DATABASE_VERSION,
    SYNC_KEY_LAST_FULL_SYNC,
    SYNC_KEY_PRINT_COUNT,
    SYNC_KEY_SET_COUNT,
    Card,
    CardPrint,
    CardSet,
    utcnow,
)
from ..logging_setup import get_logger, log_context
from ..repositories.sync_state import SyncStateRepository
from ..ygoprodeck.client import DEFAULT_PAGE_SIZE, YgoProDeckClient
from ..ygoprodeck.importer import CatalogImporter, ImportStats

log = get_logger(__name__)

#: Chamado a cada página importada: (cartas_processadas, total_estimado).
ProgressCallback = Callable[[int, int], None]


@dataclass
class SyncDecision:
    """Resultado de `check`: precisa sincronizar, e por quê."""

    needs_sync: bool
    reason: str
    remote_version: str | None = None
    remote_updated_at: str | None = None
    local_version: str | None = None


@dataclass
class SyncReport:
    """O que aconteceu em um sync."""

    performed: bool
    reason: str
    remote_version: str | None = None
    stats: ImportStats = field(default_factory=ImportStats)
    total_cards: int = 0
    total_prints: int = 0
    total_sets: int = 0
    elapsed_s: float = 0.0

    def as_dict(self) -> dict[str, object]:
        return {
            "performed": self.performed,
            "reason": self.reason,
            "remote_version": self.remote_version,
            "total_cards": self.total_cards,
            "total_prints": self.total_prints,
            "total_sets": self.total_sets,
            "elapsed_s": round(self.elapsed_s, 2),
            **self.stats.as_dict(),
        }


class SyncService:
    """Orquestra client + importer + estado, em uma transação só."""

    def __init__(
        self,
        database: Database,
        client: YgoProDeckClient,
        settings: Settings,
    ) -> None:
        self.database = database
        self.client = client
        self.settings = settings

    # ------------------------------------------------------------------ check

    def check(self) -> SyncDecision:
        """Compara a versão local com a remota, sem baixar nada."""
        remote = self.client.check_db_version()
        with self.database.session() as session:
            state = SyncStateRepository(session)
            local_version = state.get(SYNC_KEY_DATABASE_VERSION)
            has_cards = session.query(Card.id).first() is not None

        if not has_cards:
            return SyncDecision(
                needs_sync=True,
                reason="catálogo local vazio",
                remote_version=remote.database_version,
                remote_updated_at=remote.updated_at,
                local_version=local_version,
            )
        if local_version != remote.database_version:
            return SyncDecision(
                needs_sync=True,
                reason=f"versão mudou ({local_version} → {remote.database_version})",
                remote_version=remote.database_version,
                remote_updated_at=remote.updated_at,
                local_version=local_version,
            )
        return SyncDecision(
            needs_sync=False,
            reason=f"catálogo já está na versão {remote.database_version}",
            remote_version=remote.database_version,
            remote_updated_at=remote.updated_at,
            local_version=local_version,
        )

    # ------------------------------------------------------------------- sync

    def sync(
        self,
        *,
        force: bool = False,
        sets_only: bool = False,
        page_size: int = DEFAULT_PAGE_SIZE,
        progress: ProgressCallback | None = None,
    ) -> SyncReport:
        """Baixa e importa o catálogo.

        Sem `force`, respeita a decisão de `check()` e pode não fazer nada.
        """
        started = utcnow()

        decision = self.check()
        if not decision.needs_sync and not force:
            log.info("sync.skipped", reason=decision.reason)
            totals = self._totals()
            return SyncReport(
                performed=False,
                reason=decision.reason,
                remote_version=decision.remote_version,
                total_cards=totals["total_cards"],
                total_prints=totals["total_prints"],
                total_sets=totals["total_sets"],
            )

        log.info(
            "sync.start",
            reason=decision.reason if not force else "forçado pelo usuário",
            remote_version=decision.remote_version,
            sets_only=sets_only,
        )

        stats = ImportStats()
        # Uma transação para tudo: se algo falhar, o catálogo antigo continua
        # inteiro e utilizável.
        with self.database.session() as session:
            importer = CatalogImporter(session)

            api_sets = self.client.fetch_sets()
            log.info("sync.sets_fetched", count=len(api_sets))
            stats.merge(importer.import_sets(api_sets))
            session.flush()

            if not sets_only:
                known_prefixes = importer.known_set_prefixes()
                processed = 0
                total = 0

                for page, meta in self.client.iter_cards(page_size=page_size):
                    if meta is not None and meta.total_rows:
                        total = meta.total_rows
                    stats.merge(importer.import_cards(page, known_prefixes))
                    session.flush()

                    processed += len(page)
                    with log_context(processed=processed, total=total):
                        log.info("sync.page_imported", cards=len(page))
                    if progress is not None:
                        progress(processed, total or processed)

            state = SyncStateRepository(session)
            state.set_many(
                {
                    SYNC_KEY_DATABASE_VERSION: decision.remote_version,
                    SYNC_KEY_DATABASE_DATE: decision.remote_updated_at,
                    SYNC_KEY_LAST_FULL_SYNC: utcnow().isoformat(timespec="seconds"),
                }
            )
            session.flush()

            totals = {
                "total_cards": session.query(Card).count(),
                "total_prints": session.query(CardPrint).count(),
                "total_sets": session.query(CardSet).count(),
            }
            state.set_many(
                {
                    SYNC_KEY_CARD_COUNT: str(totals["total_cards"]),
                    SYNC_KEY_PRINT_COUNT: str(totals["total_prints"]),
                    SYNC_KEY_SET_COUNT: str(totals["total_sets"]),
                }
            )

        if stats.unparsed_set_codes:
            sample = sorted(set(stats.unparsed_set_codes))[:10]
            log.warning(
                "sync.unparsed_set_codes",
                count=len(set(stats.unparsed_set_codes)),
                sample=sample,
            )

        elapsed = (utcnow() - started).total_seconds()
        log.info("sync.done", elapsed_s=round(elapsed, 1), **stats.as_dict())

        return SyncReport(
            performed=True,
            reason=decision.reason if not force else "forçado pelo usuário",
            remote_version=decision.remote_version,
            stats=stats,
            elapsed_s=elapsed,
            total_cards=totals["total_cards"],
            total_prints=totals["total_prints"],
            total_sets=totals["total_sets"],
        )

    # ---------------------------------------------------------------- helpers

    def _totals(self) -> dict[str, int]:
        with self.database.session() as session:
            return {
                "total_cards": session.query(Card).count(),
                "total_prints": session.query(CardPrint).count(),
                "total_sets": session.query(CardSet).count(),
            }
