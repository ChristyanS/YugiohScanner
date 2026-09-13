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
from typing import Any

from sqlalchemy.orm import Session

from ..config import Settings
from ..db.session import Database
from ..db.tables import (
    DEFAULT_CONDITION,
    DEFAULT_EDITION,
    DEFAULT_LANGUAGE,
    CardPrint,
    CollectionItem,
    ScanImage,
    ScanJob,
    ScanResult,
)
from ..domain.confidence import Decision
from ..errors import (
    JobNotFoundError,
    ScanImageNotFoundError,
    ScanResultAlreadyAppliedError,
    ScanResultNotFoundError,
    YugiohScannerError,
)
from ..images.grid import ensure_cv_available
from ..images.preprocess import BoundingBox, ImageError, load_image, prepare_regions
from ..logging_setup import get_logger, log_context
from ..matching.candidates import NameIndex
from ..matching.engine import MatchingEngine, MatchResult
from ..matching.print_language import (
    CompositePrintLanguageResolver,
    OverridePrintLanguageResolver,
    PrintLanguageResolver,
    SiblingPrintLanguageResolver,
)
from ..ocr.base import REGION_CODE, REGION_FULL, REGION_NAME, OCRProvider, OCRRequest
from ..ocr.registry import create_provider
from ..repositories.collection import CollectionKey, CollectionRepository
from ..repositories.print_override import PrintOverrideRepository
from ..repositories.scans import ScanRepository
from ..scanner.discovery import DiscoveredImage, discover_images, resolve_scan_folder
from ..scanner.executor import resolve_workers
from ..scanner.pipeline import run_pipeline
from ..scanner.worker import CropOutcome, CropRegion, ScanOutcome

log = get_logger(__name__)

#: A cada quantas imagens processadas o commit acontece.
#:
#: Nem por imagem (custo de fsync repetido, plano §20.1) nem em uma transação
#: só para a pasta inteira (um crash no meio perderia tudo já processado e
#: forçaria reler o pacote inteiro no próximo `scan`). Em lote é o meio-termo
#: do plano §20.3.
COMMIT_BATCH_SIZE = 25

#: Estados de decisão que só existem depois de revisão humana — fora do
#: enum `Decision` de propósito (ver `confirm_result`/`reject_result`).
_DECISION_CONFIRMED = "confirmed"
_DECISION_REJECTED = "rejected"


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
    #: `None` em dry-run e para imagens puladas/falhas — nada foi persistido.
    #: Presente quando `decision` é `pending`/`manual`/`unmatched`: é por este
    #: ID que `scan review` (Fase 6) confirma ou rejeita a leitura depois.
    result_id: int | None = None
    #: Posição deste recorte dentro da foto de origem e quantos recortes ela
    #: rendeu (plano §22). `0`/`1` é o caso de hoje — uma foto, uma carta.
    crop_index: int = 0
    crop_count: int = 1

    def as_dict(self) -> dict[str, object]:
        return {
            "file": self.file_name,
            "status": self.status,
            "skipped": self.skipped,
            "decision": self.decision,
            "result_id": self.result_id,
            "ocr_name": self.ocr_name,
            "ocr_code": self.ocr_code,
            "name": self.card_name,
            "set_code": self.set_code,
            "confidence": None if self.confidence is None else round(self.confidence, 4),
            "applied": self.applied,
            "error": self.error,
            "crop_index": self.crop_index,
            "crop_count": self.crop_count,
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

    def __init__(
        self,
        database: Database,
        settings: Settings,
        *,
        language_resolver: PrintLanguageResolver | None = None,
    ) -> None:
        self.database = database
        self.settings = settings
        # Um índice por instância do serviço: reaproveitado entre imagens do
        # mesmo scan (e entre scans sucessivos, se o serviço for reutilizado).
        # Recarregar os ~14,5k nomes por imagem custaria a busca inteira de
        # novo a cada foto (plano §20.2).
        self._name_index = NameIndex()
        # Injetável de propósito (plano de idiomas). Default = cadeia (ADR
        # 0012): overrides aprendidos do próprio uso (Opção D) vencem antes
        # de tentar achar print irmão no catálogo sincronizado (ADR 0009,
        # Opção B). Uma correção manual do usuário sempre tem prioridade
        # sobre a heurística automática.
        self.language_resolver = language_resolver or CompositePrintLanguageResolver(
            [OverridePrintLanguageResolver(), SiblingPrintLanguageResolver()]
        )
        # Cascata de fallback (plano §6.3, Fase 10): criada sob demanda, uma
        # vez por serviço. `_fallback_checked` distingue "ainda não tentei"
        # de "tentei e não deu" — sem isso, cada imagem duvidosa repetiria a
        # falha (chave ausente, pacote não instalado) em vez de degradar uma
        # única vez e seguir em silêncio pelo resto do scan (ADR 0004).
        self._fallback_provider: OCRProvider | None = None
        self._fallback_checked = False

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
        grid: bool = False,
        grid_size: tuple[int, int] | None = None,
        progress: ProgressCallback | None = None,
    ) -> ScanRunReport:
        started = time.perf_counter()
        # Informar `grid_size` ("--grid-size 3x3") já liga o modo grade
        # sozinho — não faz sentido pedir os dois separadamente.
        effective_grid = grid or grid_size is not None
        if effective_grid and grid_size is None:
            # Falha cedo e uma única vez, antes de tocar no banco — não por
            # imagem lá no worker, onde daria N erros idênticos (plano §22).
            # Só quando vai mesmo detectar por contorno: `grid_size` explícito
            # não usa `cv2` (é aritmética + a mesma heurística de borda
            # Pillow-only de `detect_card_bounds`), então não precisa do
            # extra `cv` instalado.
            ensure_cv_available()
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
                grid=effective_grid,
                grid_size=grid_size,
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

    # ------------------------------------------------------------------ revisão

    def pending_results(self, limit: int = 100) -> list[ScanResult]:
        """A fila de revisão (plano §12): leituras que esperam um humano."""
        with self.database.session() as session:
            results = ScanRepository(session).pending_results(limit=limit)
            # Força o carregamento antes da sessão fechar — `scan_image` é
            # `lazy` por padrão e o chamador (CLI) acessa fora deste `with`.
            for result in results:
                _ = result.scan_image.file_path
            return results

    def get_image_path(self, scan_image_id: int) -> Path:
        """Caminho no disco da foto original (Fase 8: `/review` mostra a
        foto ao lado da leitura — nunca aceita caminho vindo do cliente, só
        o ID, resolvido aqui contra o que o próprio scan gravou (plano §21)."""
        with self.database.session() as session:
            image = session.get(ScanImage, scan_image_id)
            if image is None:
                raise ScanImageNotFoundError(scan_image_id)
            return Path(image.file_path)

    def get_result(self, result_id: int) -> ScanResult:
        """Detalhe de uma leitura (Fase 8: `GET /api/v1/scan-results/{id}`)."""
        with self.database.session() as session:
            result = ScanRepository(session).get_result(result_id)
            if result is None:
                raise ScanResultNotFoundError(result_id)
            _ = result.scan_image.file_path
            session.expunge(result)
            return result

    def confirm_result(
        self,
        result_id: int,
        *,
        card_id: int,
        card_print_id: int | None = None,
        quantity: int = 1,
        language: str | None = None,
    ) -> CollectionItem:
        """Aplica manualmente um resultado pendente/manual (revisão humana).

        É o mesmo destino de uma decisão `auto`, só que decidido por uma
        pessoa em vez da política de confiança — por isso passa pela mesma
        `CollectionRepository.add_copies` e fica marcado `applied=True`,
        preservando a garantia de que uma imagem só conta uma vez (§13.2).

        `language` explícito (a tela de revisão manda o que está selecionado
        no momento da confirmação) tem prioridade; omitido, cai para o
        idioma que o matching já havia detectado (`ScanResult.detected_language`)
        e só então para `DEFAULT_LANGUAGE` — nunca fica sem valor.
        """
        with self.database.session() as session:
            scan_repo = ScanRepository(session)
            collection_repo = CollectionRepository(session)

            result = scan_repo.get_result(result_id)
            if result is None:
                raise ScanResultNotFoundError(result_id)
            if result.applied:
                raise ScanResultAlreadyAppliedError(result_id)

            resolved_language = language or result.detected_language or DEFAULT_LANGUAGE
            key = CollectionKey(
                card_id=card_id,
                card_print_id=card_print_id,
                condition=DEFAULT_CONDITION,
                edition=DEFAULT_EDITION,
                language=resolved_language,
            )
            item = collection_repo.add_copies(key, quantity, source="scan")

            # ADR 0012 (Opção D): um humano acabou de dizer, com autoridade,
            # "esta é a carta+print certos para este idioma". Gravar isso
            # ensina o app pra próxima vez, mesmo quando nem a YGOPRODeck nem
            # o enriquecimento `yaml-yugi` sabiam a resposta.
            if card_print_id is not None and resolved_language and resolved_language != "EN":
                PrintOverrideRepository(session).set(card_id, resolved_language, card_print_id)
            # "confirmed" não é uma saída da política de confiança (por isso
            # não está no enum `Decision`, que é a saída pura de
            # `domain/confidence.py`) — é um estado só de revisão humana,
            # já previsto no CHECK do schema (`SCAN_DECISIONS`).
            result.decision = _DECISION_CONFIRMED
            result.card_id = card_id
            result.card_print_id = card_print_id
            scan_repo.mark_applied(result, item.id)

            # Este método abre e fecha a própria sessão: sem tocar `.card`
            # (relação `lazy="joined"`, mas só populada de fato no primeiro
            # acesso) agora, o objeto devolvido ficaria "detached" e o
            # chamador levaria `DetachedInstanceError` ao ler `item.card.name`.
            _ = item.card
            _ = item.card_print
            session.expunge(item)
            return item

    def reject_result(self, result_id: int) -> None:
        """Descarta uma leitura pendente sem tocar na coleção.

        Não reprocessa: a `ScanImage` continua marcada como vista, então um
        `scan` futuro na mesma pasta não volta a perguntar sobre ela.
        """
        with self.database.session() as session:
            scan_repo = ScanRepository(session)
            result = scan_repo.get_result(result_id)
            if result is None:
                raise ScanResultNotFoundError(result_id)
            scan_repo.mark_decided(result, decision=_DECISION_REJECTED)

    # -------------------------------------------------------------------- jobs

    def recent_jobs(self, limit: int = 10) -> list[ScanJob]:
        with self.database.session() as session:
            jobs = ScanRepository(session).recent_jobs(limit=limit)
            session.expunge_all()
            return jobs

    def job_detail(self, job_id: int) -> ScanJob:
        with self.database.session() as session:
            job = ScanRepository(session).get_job(job_id)
            if job is None:
                raise JobNotFoundError(job_id)
            session.expunge(job)
            return job

    def job_results(self, job_id: int) -> list[ScanResult]:
        """Os resultados de um job (Fase 8: `GET /scan/{id}` e `/api/v1/scans/{id}/results`)."""
        with self.database.session() as session:
            if ScanRepository(session).get_job(job_id) is None:
                raise JobNotFoundError(job_id)
            results = ScanRepository(session).results_for_job(job_id)
            for result in results:
                _ = result.scan_image.file_path
            session.expunge_all()
            return results

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
        grid: bool = False,
        grid_size: tuple[int, int] | None = None,
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
        outcomes = run_pipeline(
            to_process,
            self.settings,
            provider_name=provider,
            workers=workers,
            grid_mode=grid,
            grid_size=grid_size,
        )

        job_id = job.id if job is not None else None

        since_commit = 0
        for outcome in outcomes:
            with log_context(image=outcome.path.name):
                crop_events = self._handle_outcome(
                    outcome,
                    job_id=job_id,
                    scan_repo=scan_repo,
                    collection_repo=collection_repo,
                    engine=engine,
                    report=report,
                    apply=apply,
                    no_auto=no_auto,
                )
            report.events.extend(crop_events)
            for event in crop_events:
                if progress is not None:
                    progress(event)

            since_commit += len(crop_events)
            if apply and since_commit >= COMMIT_BATCH_SIZE:
                session.commit()
                since_commit = 0

        if apply and job is not None:
            scan_repo.finish_job(
                job,
                status="done",
                total_images=report.total_images,
                processed=report.processed,
                skipped=report.skipped,
                auto_added=report.auto_added,
                pending=report.pending,
                failed=report.failed,
            )

    def _upsert_scan_image(
        self,
        outcome: ScanOutcome,
        scan_repo: ScanRepository,
        job_id: int,
        *,
        status: str,
        ocr_raw: dict[str, Any] | None,
    ) -> ScanImage:
        existing = scan_repo.find_by_hash(outcome.task.file_hash)
        if existing is None:
            return scan_repo.create_image(
                job_id=job_id,
                file_path=str(outcome.path),
                file_hash=outcome.task.file_hash,
                file_size=outcome.task.size,
                status=status,
                error=outcome.preprocess_error,
                ocr_raw=ocr_raw,
                ocr_ms=outcome.elapsed_ms,
            )
        scan_repo.update_image_reading(
            existing,
            status=status,
            ocr_raw=ocr_raw,
            ocr_ms=outcome.elapsed_ms,
            error=outcome.preprocess_error,
        )
        return existing

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
    ) -> list[ImageEvent]:
        """Bookkeeping por foto; o matching/decisão de cada recorte é
        delegado a `_handle_crop` — uma foto sem grade rende exatamente um
        recorte (`CropRegion(0, 1, None)`), então o caminho de hoje (uma
        foto = uma carta) produz exatamente um `ImageEvent`, como sempre.
        """
        report.processed += 1

        if outcome.preprocess_status is not None:
            # A foto inteira falhou antes de qualquer recorte (arquivo
            # corrompido, formato inválido) — `crops` nunca chegou a existir.
            if apply:
                assert job_id is not None  # `apply=True` sempre cria o job em `_run`
                self._upsert_scan_image(
                    outcome, scan_repo, job_id, status=outcome.preprocess_status, ocr_raw=None
                )
            report.failed += 1
            return [
                ImageEvent(
                    file_name=outcome.path.name,
                    status=outcome.preprocess_status,
                    error=outcome.preprocess_error,
                )
            ]

        image = None
        if apply:
            assert job_id is not None
            ocr_raw = {
                "crops": [crop.ocr.as_dict() for crop in outcome.crops if crop.ocr is not None]
            }
            image_status = (
                "ok" if any(not crop.failed for crop in outcome.crops) else outcome.crops[0].status
            )
            image = self._upsert_scan_image(
                outcome, scan_repo, job_id, status=image_status, ocr_raw=ocr_raw
            )

        return [
            self._handle_crop(
                crop,
                outcome,
                image,
                engine=engine,
                report=report,
                apply=apply,
                no_auto=no_auto,
                scan_repo=scan_repo,
                collection_repo=collection_repo,
            )
            for crop in outcome.crops
        ]

    def _handle_crop(
        self,
        crop: CropOutcome,
        outcome: ScanOutcome,
        image: ScanImage | None,
        *,
        engine: MatchingEngine,
        report: ScanRunReport,
        apply: bool,
        no_auto: bool,
        scan_repo: ScanRepository,
        collection_repo: CollectionRepository,
    ) -> ImageEvent:
        if crop.failed or crop.status == "ocr_empty":
            report.failed += 1
            return ImageEvent(
                file_name=outcome.path.name,
                status=crop.status,
                error=crop.error,
                crop_index=crop.region.index,
                crop_count=crop.region.count,
            )

        match_result = engine.match(crop.read_name, crop.read_code or None)
        decision = match_result.decision

        # Cascata (plano §6.3): só a fração duvidosa reprocessa por LLM —
        # decisão `auto` já está resolvida e não vale o custo/latência extra.
        if decision != Decision.AUTO:
            fallback = self._try_fallback(outcome.path, crop.region, engine)
            if fallback is not None and fallback.confidence >= match_result.confidence:
                match_result = fallback
                decision = fallback.decision

        if no_auto and decision == Decision.AUTO:
            decision = Decision.PENDING

        applied = False
        result_id: int | None = None
        if apply and image is not None:
            applied, result_id = self._apply_decision(
                image.id,
                decision,
                match_result,
                ocr_name_raw=crop.read_name or None,
                ocr_code_raw=crop.read_code or None,
                scan_repo=scan_repo,
                collection_repo=collection_repo,
                crop_index=crop.region.index,
                crop_count=crop.region.count,
                source_bbox=crop.region.bbox,
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
            status=crop.status,
            decision=decision.value,
            result_id=result_id,
            ocr_name=crop.read_name or None,
            ocr_code=crop.read_code or None,
            card_name=match_result.card_name,
            set_code=match_result.set_code,
            confidence=match_result.confidence,
            applied=applied,
            crop_index=crop.region.index,
            crop_count=crop.region.count,
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
        crop_index: int = 0,
        crop_count: int = 1,
        source_bbox: BoundingBox | None = None,
    ) -> tuple[bool, int]:
        """Grava o `ScanResult` e, se for o caso, soma a cópia na coleção.

        Devolve `(aplicado, result_id)` — o ID é sempre gravado (mesmo quando
        `aplicado=False`), pois é por ele que `scan review` confirma ou
        rejeita a leitura depois. `aplicado=False` também no caso legítimo de
        `--reprocess` que já havia aplicado antes (plano §13.2).
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
            detected_language=match_result.matched_language,
            crop_index=crop_index,
            crop_count=crop_count,
            source_bbox=source_bbox,
        )

        if decision != Decision.AUTO or match_result.card_id is None:
            return False, result.id

        # Guarda de idempotência: este recorte já contribuiu antes? Isso só
        # pode acontecer sob --reprocess — uma imagem nova nunca tem
        # resultado anterior aplicado. Filtrado por `crop_index` porque numa
        # foto com grade cada recorte é uma carta diferente (plano §22).
        if scan_repo.has_applied_result(scan_image_id, crop_index=crop_index):
            log.info(
                "scan.reprocess_not_reapplied", scan_image_id=scan_image_id, crop_index=crop_index
            )
            return False, result.id

        # Sem revisão humana aqui — é a alta confiança que dispensa isso —
        # então é o único lugar onde vale a pena *tentar* trocar para o print
        # no idioma detectado sozinho. Na esmagadora maioria dos casos
        # (DE/FR/IT) não existe print irmão e nada muda; quando existe (PT/
        # OTS), a carta entra na coleção já com o código certo.
        print_id = match_result.card_print_id
        language = match_result.matched_language or DEFAULT_LANGUAGE
        if match_result.matched_language and match_result.matched_language != "EN":
            current_print = (
                collection_repo.session.get(CardPrint, print_id) if print_id is not None else None
            )
            swapped = self.language_resolver.resolve(
                collection_repo.session, match_result.card_id, current_print, language
            )
            if swapped is not None:
                print_id = swapped.id
                # O registro de auditoria também reflete o print de verdade
                # usado — senão `ScanResult.card_print_id` mentiria sobre o
                # que a coleção recebeu.
                result.card_print_id = print_id

        key = CollectionKey(
            card_id=match_result.card_id,
            card_print_id=print_id,
            condition=DEFAULT_CONDITION,
            edition=DEFAULT_EDITION,
            language=language,
        )
        item = collection_repo.add_copies(key, 1, source="scan")
        scan_repo.mark_applied(result, item.id)
        return True, result.id

    # -------------------------------------------------------- fallback LLM

    def _get_fallback_provider(self) -> OCRProvider | None:
        """Provider da cascata (plano §6.3), criado e aquecido uma vez.

        `YGS_OCR_FALLBACK_PROVIDER=none` (padrão) desliga a cascata sem
        custo algum — nem o módulo do provider é importado. Qualquer falha
        ao criar/aquecer (pacote ausente, chave ausente, API fora do ar na
        primeira tentativa) desliga a cascata pelo resto deste `ScanService`
        e loga uma vez, nunca por imagem — é o "degrada em silêncio" do
        ADR 0004, não um erro que derruba o scan.
        """
        if self._fallback_checked:
            return self._fallback_provider

        self._fallback_checked = True
        name = self.settings.ocr_fallback_provider
        if not name or name == "none":
            return None

        try:
            provider = create_provider(name, self.settings)
            provider.warmup()
        except YugiohScannerError as exc:
            log.info("scan.fallback_unavailable", provider=name, reason=str(exc))
            return None

        self._fallback_provider = provider
        return provider

    def _try_fallback(
        self, path: Path, region: CropRegion, engine: MatchingEngine
    ) -> MatchResult | None:
        """Reprocessa uma leitura duvidosa com o provider de fallback.

        Reabre e prepara a imagem de novo (o worker já fechou a sua cópia —
        plano §2.3, workers não sobrevivem além do próprio processamento) e
        manda a região `full` — a carta inteira recortada/reduzida, nunca a
        original (plano §6.4). Recorta para `region.bbox` antes de preparar,
        senão uma foto com grade (plano §22) reprocessaria a página inteira
        no lugar da célula duvidosa. Roda sequencialmente no processo
        principal: só a fração `pending`/`manual` chega aqui (~15% do plano
        §6.3), e cada chamada já é I/O-bound por natureza; paralelizar isso
        fica para quando o volume justificar (ver `Settings.llm_concurrency`,
        ainda não usado aqui — reservado para uma futura versão em lote/
        Batches API, plano §6.4, não implementada nesta fase).
        """
        provider = self._get_fallback_provider()
        if provider is None:
            return None

        try:
            image = load_image(path, max_pixels=self.settings.max_image_pixels)
        except ImageError:
            return None

        try:
            prepared = prepare_regions(image, source=path, region=region.bbox)
        except ImageError:
            return None
        finally:
            image.close()

        try:
            full = prepared.regions.get(REGION_FULL)
            if full is None:
                return None
            request = OCRRequest(regions={REGION_FULL: full}, source=str(path))
            result = provider.read(request)
        except YugiohScannerError as exc:
            log.info("scan.fallback_failed", file=path.name, reason=str(exc))
            return None
        finally:
            prepared.close()

        name = result.joined(REGION_NAME) or result.joined(REGION_FULL)
        code = result.best(REGION_CODE)
        fallback_match = engine.match(name, code or None)
        log.info(
            "scan.fallback_used",
            file=path.name,
            crop_index=region.index,
            confidence=round(fallback_match.confidence, 3),
        )
        return fallback_match
