"""Fecha o ciclo scan → coleção, com idempotência real (plano §13 e Fase 5).

Três garantias que este serviço é responsável por manter, e que os testes de
idempotência verificam diretamente:

1. Rodar `scan` duas vezes na mesma pasta não altera nenhuma quantidade —
   imagens já vistas (por hash) são puladas, a menos que `--reprocess`.
2. Uma imagem nunca contribui duas vezes para a coleção, mesmo reprocessada.
3. `--dry-run` não grava **nada**: nem `ScanJob`, nem `ScanImage`, nem
   `ScanResult`, nem coleção. É preview puro.

O caminho de OCR + matching é o mesmo para dry-run e para execução real — só a
persistência é condicional. Isso evita a bifurcação de lógica que costuma
fazer o modo "preview" mentir sobre o que o modo real faria.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.orm import Session

from ..config import Settings
from ..db.session import Database
from ..db.tables import DEFAULT_CONDITION, DEFAULT_EDITION, DEFAULT_LANGUAGE
from ..domain.confidence import Decision
from ..logging_setup import get_logger, log_context
from ..matching.candidates import NameIndex
from ..matching.engine import MatchingEngine, MatchResult
from ..repositories.collection import CollectionKey, CollectionRepository
from ..repositories.scans import ScanRepository
from ..scanner.discovery import DiscoveredImage, discover_images, resolve_scan_folder
from ..scanner.executor import resolve_workers
from ..scanner.pipeline import run_pipeline
from ..scanner.worker import ScanOutcome

log = get_logger(__name__)

#: A cada quantas imagens processadas o commit acontece.
#:
#: Nem por imagem (custo de fsync repetido, plano §20.1) nem em uma transação
#: só para a pasta inteira (um crash no meio perderia tudo já processado e
#: forçaria reler o pacote inteiro no próximo `scan`). Em lote é o meio-termo
#: do plano §20.3.
COMMIT_BATCH_SIZE = 25


@dataclass(frozen=True, slots=True)
class ImageEvent:
    """Emitido depois de cada imagem — para barra de progresso, log e --json."""

    file_name: str
    #: Status do OCR/pré-processamento: ok, invalid, error, ocr_empty.
    status: str
    skipped: bool = False
    decision: str | None = None
    #: Texto **bruto** que o OCR leu — o que o plano §12 chama de "resultado
    #: do OCR", distinto da carta identificada. Sem isto não dava para
    #: diagnosticar por que uma leitura ficou `manual` sem reabrir a foto.
    ocr_name: str | None = None
    ocr_code: str | None = None
    #: A carta/print **resolvidos** no catálogo — pode divergir bastante do
    #: texto bruto (ou ficar `None` quando nada casou).
    card_name: str | None = None
    set_code: str | None = None
    confidence: float | None = None
    applied: bool = False
    error: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "file": self.file_name,
            "status": self.status,
            "skipped": self.skipped,
            "decision": self.decision,
            "ocr_name": self.ocr_name,
            "ocr_code": self.ocr_code,
            "name": self.card_name,
            "set_code": self.set_code,
            "confidence": None if self.confidence is None else round(self.confidence, 4),
            "applied": self.applied,
            "error": self.error,
        }


@dataclass
class ScanRunReport:
    """O que aconteceu numa execução de `scan`."""

    folder: Path
    #: `None` em dry-run: nenhum job é persistido.
    job_id: int | None = None
    provider_name: str | None = None
    workers: int | None = None
    total_images: int = 0
    skipped: int = 0
    processed: int = 0
    auto_added: int = 0
    pending: int = 0
    failed: int = 0
    elapsed_s: float = 0.0
    events: list[ImageEvent] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "folder": str(self.folder),
            "job_id": self.job_id,
            "provider": self.provider_name,
            "workers": self.workers,
            "total_images": self.total_images,
            "skipped": self.skipped,
            "processed": self.processed,
            "auto_added": self.auto_added,
            "pending": self.pending,
            "failed": self.failed,
            "elapsed_s": round(self.elapsed_s, 2),
            "results": [event.as_dict() for event in self.events],
        }


ProgressCallback = Callable[[ImageEvent], None]


class ScanService:
    """Orquestra descoberta + OCR + matching + coleção, com controle de sessão."""

    def __init__(self, database: Database, settings: Settings) -> None:
        self.database = database
        self.settings = settings
        # Um índice por instância do serviço: reaproveitado entre imagens do
        # mesmo scan (e entre scans sucessivos, se o serviço for reutilizado).
        # Recarregar os ~14,5k nomes por imagem custaria a busca inteira de
        # novo a cada foto (plano §20.2).
        self._name_index = NameIndex()

    def scan(
        self,
        folder: Path | str,
        *,
        provider_name: str | None = None,
        workers: int | None = None,
        recursive: bool = False,
        limit: int | None = None,
        apply: bool = True,
        no_auto: bool = False,
        reprocess: bool = False,
        progress: ProgressCallback | None = None,
    ) -> ScanRunReport:
        started = time.perf_counter()
        resolved_folder = resolve_scan_folder(folder, self.settings)
        discovered = discover_images(resolved_folder, recursive=recursive, limit=limit)

        report = ScanRunReport(
            folder=resolved_folder,
            total_images=len(discovered),
            provider_name=provider_name or self.settings.ocr_provider,
            workers=resolve_workers(self.settings, workers),
        )

        if not discovered:
            report.elapsed_s = time.perf_counter() - started
            return report

        session = self.database.session_factory()
        try:
            self._run(
                session,
                discovered,
                report,
                provider_name=provider_name,
                workers=workers,
                apply=apply,
                no_auto=no_auto,
                reprocess=reprocess,
                progress=progress,
            )
            if apply:
                session.commit()
            else:
                # Dry-run: mesmo que nada tenha sido escrito por construção,
                # reverter explicitamente é a garantia de que uma consulta de
                # leitura não deixa nenhum estado pendurado na sessão.
                session.rollback()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

        report.elapsed_s = time.perf_counter() - started
        log.info(
            "scan.done",
            folder=str(resolved_folder),
            apply=apply,
            elapsed_s=round(report.elapsed_s, 1),
            total=report.total_images,
            skipped=report.skipped,
            auto_added=report.auto_added,
            pending=report.pending,
            failed=report.failed,
        )
        return report

    # ---------------------------------------------------------------- internos

    def _run(
        self,
        session: Session,
        discovered: list[DiscoveredImage],
        report: ScanRunReport,
        *,
        provider_name: str | None,
        workers: int | None,
        apply: bool,
        no_auto: bool,
        reprocess: bool,
        progress: ProgressCallback | None,
    ) -> None:
        scan_repo = ScanRepository(session)
        collection_repo = CollectionRepository(session)
        engine = MatchingEngine(session, self.settings, index=self._name_index)

        known_hashes = scan_repo.known_hashes([image.file_hash for image in discovered])

        to_process: list[DiscoveredImage] = []
        for image in discovered:
            already_known = image.file_hash in known_hashes
            if already_known and not reprocess:
                report.skipped += 1
                event = ImageEvent(file_name=image.path.name, status="skipped", skipped=True)
                report.events.append(event)
                if progress is not None:
                    progress(event)
                continue
            to_process.append(image)

        if not to_process:
            return

        job = None
        if apply:
            job = scan_repo.create_job(
                folder_path=str(report.folder),
                ocr_provider=provider_name or self.settings.ocr_provider,
                workers=workers or self.settings.effective_workers,
            )
            report.job_id = job.id

        provider = provider_name or self.settings.ocr_provider
        outcomes = run_pipeline(to_process, self.settings, provider_name=provider, workers=workers)

        job_id = job.id if job is not None else None

        since_commit = 0
        for outcome in outcomes:
            with log_context(image=outcome.path.name):
                event = self._handle_outcome(
                    outcome,
                    job_id=job_id,
                    scan_repo=scan_repo,
                    collection_repo=collection_repo,
                    engine=engine,
                    report=report,
                    apply=apply,
                    no_auto=no_auto,
                )
            report.events.append(event)
            if progress is not None:
                progress(event)

            since_commit += 1
            if apply and since_commit >= COMMIT_BATCH_SIZE:
                session.commit()
                since_commit = 0

        if apply and job is not None:
            scan_repo.finish_job(job, status="done")

    def _handle_outcome(
        self,
        outcome: ScanOutcome,
        *,
        job_id: int | None,
        scan_repo: ScanRepository,
        collection_repo: CollectionRepository,
        engine: MatchingEngine,
        report: ScanRunReport,
        apply: bool,
        no_auto: bool,
    ) -> ImageEvent:
        report.processed += 1

        image = None
        if apply:
            assert job_id is not None  # `apply=True` sempre cria o job em `_run`
            existing = scan_repo.find_by_hash(outcome.task.file_hash)
            ocr_raw = outcome.ocr.as_dict() if outcome.ocr else None
            if existing is None:
                image = scan_repo.create_image(
                    job_id=job_id,
                    file_path=str(outcome.path),
                    file_hash=outcome.task.file_hash,
                    file_size=outcome.task.size,
                    status=outcome.status,
                    error=outcome.error,
                    ocr_raw=ocr_raw,
                    ocr_ms=outcome.elapsed_ms,
                )
            else:
                scan_repo.update_image_reading(
                    existing,
                    status=outcome.status,
                    ocr_raw=ocr_raw,
                    ocr_ms=outcome.elapsed_ms,
                    error=outcome.error,
                )
                image = existing

        if outcome.failed or outcome.status == "ocr_empty":
            report.failed += 1
            return ImageEvent(
                file_name=outcome.path.name,
                status=outcome.status,
                error=outcome.error,
            )

        match_result = engine.match(outcome.read_name, outcome.read_code or None)
        decision = match_result.decision
        if no_auto and decision == Decision.AUTO:
            decision = Decision.PENDING

        applied = False
        if apply and image is not None:
            applied = self._apply_decision(
                image.id,
                decision,
                match_result,
                ocr_name_raw=outcome.read_name or None,
                ocr_code_raw=outcome.read_code or None,
                scan_repo=scan_repo,
                collection_repo=collection_repo,
            )

        # `applied` só significa algo em `apply=True` (é a gravação de verdade
        # na coleção). Em dry-run ele é sempre False — nada é jamais aplicado
        # — então contar por `applied` faria o preview mentir, mostrando 0
        # adições automáticas mesmo quando a decisão é `auto`. Em dry-run,
        # "seria adicionada" é só a decisão; em modo real, só conta o que de
        # fato foi gravado (por isso a guarda de `--reprocess` continua
        # funcionando: decisão auto + não reaplicada cai em pending).
        counts_as_auto_added = decision == Decision.AUTO and (applied or not apply)
        if counts_as_auto_added:
            report.auto_added += 1
        else:
            report.pending += 1

        return ImageEvent(
            file_name=outcome.path.name,
            status=outcome.status,
            decision=decision.value,
            ocr_name=outcome.read_name or None,
            ocr_code=outcome.read_code or None,
            card_name=match_result.card_name,
            set_code=match_result.set_code,
            confidence=match_result.confidence,
            applied=applied,
        )

    def _apply_decision(
        self,
        scan_image_id: int,
        decision: Decision,
        match_result: MatchResult,
        *,
        ocr_name_raw: str | None,
        ocr_code_raw: str | None,
        scan_repo: ScanRepository,
        collection_repo: CollectionRepository,
    ) -> bool:
        """Grava o `ScanResult` e, se for o caso, soma a cópia na coleção.

        Devolve se a cópia foi de fato aplicada — `False` também no caso
        legítimo de `--reprocess` que já havia aplicado antes (plano §13.2).
        """
        result = scan_repo.create_result(
            scan_image_id,
            card_id=match_result.card_id,
            card_print_id=match_result.card_print_id,
            # O texto **bruto** do OCR — não o nome/código já resolvidos no
            # catálogo. É o que a tela de revisão (Fase 6/8) precisa mostrar
            # ao lado da carta identificada para o usuário julgar a leitura.
            ocr_name_raw=ocr_name_raw,
            ocr_code_raw=ocr_code_raw,
            name_score=match_result.name_score,
            code_score=match_result.code_score,
            confidence=match_result.confidence,
            margin=match_result.margin,
            candidates=match_result.candidates or None,
            decision=decision.value,
        )

        if decision != Decision.AUTO or match_result.card_id is None:
            return False

        # Guarda de idempotência: esta imagem já contribuiu antes? Isso só
        # pode acontecer sob --reprocess — uma imagem nova nunca tem
        # resultado anterior aplicado.
        if scan_repo.has_applied_result(scan_image_id):
            log.info("scan.reprocess_not_reapplied", scan_image_id=scan_image_id)
            return False

        key = CollectionKey(
            card_id=match_result.card_id,
            card_print_id=match_result.card_print_id,
            condition=DEFAULT_CONDITION,
            edition=DEFAULT_EDITION,
            language=DEFAULT_LANGUAGE,
        )
        item = collection_repo.add_copies(key, 1, source="scan")
        scan_repo.mark_applied(result, item.id)
        return True
