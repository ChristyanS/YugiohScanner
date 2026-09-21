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

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings
from ..db.session import Database
from ..db.tables import (
    DEFAULT_CONDITION,
    DEFAULT_EDITION,
    DEFAULT_LANGUAGE,
    Card,
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
from ..matching.resolver import PrintResolver
from ..ocr.base import (
    REGION_CODE,
    REGION_FULL,
    REGION_NAME,
    REGION_PASSCODE,
    OCRProvider,
    OCRRequest,
)
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
    ocr_passcode: str | None = None
    #: A carta/print **resolvidos** no catálogo — pode divergir bastante do
    #: texto bruto (ou ficar `None` quando nada casou).
    card_name: str | None = None
    set_code: str | None = None
    confidence: float | None = None
    #: A identidade veio do passcode validado — a fonte mais forte que existe
    #: (`matching/engine.py::_from_passcode`). Transparência para CLI/--json.
    passcode_verified: bool = False
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
            "ocr_passcode": self.ocr_passcode,
            "name": self.card_name,
            "set_code": self.set_code,
            "confidence": None if self.confidence is None else round(self.confidence, 4),
            "passcode_verified": self.passcode_verified,
            "applied": self.applied,
            "error": self.error,
            "crop_index": self.crop_index,
            "crop_count": self.crop_count,
        }


@dataclass(frozen=True, slots=True)
class FallbackReading:
    """Resultado de uma tentativa da cascata LLM (plano §6.3).

    Carrega o texto **bruto** que o provider de fallback leu junto do
    `MatchResult` — quando esta leitura é adotada, é este texto (não o do
    worker original) que precisa virar `ScanResult.ocr_name_raw`/`ocr_code_raw`,
    senão a revisão mostra "(vazio)" ao lado de uma carta que só foi
    identificada graças ao fallback.
    """

    match: MatchResult
    name_raw: str
    code_raw: str
    passcode_raw: str = ""


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
    #: Dos `auto_added` acima, quantos eram `--reprocess` de um recorte que
    #: já tinha sido aplicado num scan anterior (`has_applied_result`) — não
    #: escreveram nada novo na coleção. Informativo só; não é subtraído de
    #: `auto_added` (a decisão do motor foi `auto` de verdade, é isso que
    #: `auto_added` reporta) nem somado a `pending` (não precisa de revisão
    #: nenhuma — plano §13.2).
    already_applied: int = 0
    pending: int = 0
    failed: int = 0
    #: Células de uma grade `--grid-size` descartadas por estarem vazias —
    #: nunca viraram `ScanResult`, então não entram em `pending`/`failed`.
    skipped_empty: int = 0
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
            "already_applied": self.already_applied,
            "pending": self.pending,
            "failed": self.failed,
            "skipped_empty": self.skipped_empty,
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
        require_set: bool | None = None,
        require_rarity: bool | None = None,
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
                require_set=require_set,
                require_rarity=require_rarity,
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
            skipped_empty=report.skipped_empty,
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
            self._attach_code_prints(session, results)
            self._attach_card_names(session, results)
            return results

    def _attach_code_prints(self, session: Session, results: list[ScanResult]) -> None:
        """Anexa a cada leitura os prints que o código bruto resolve **hoje**.

        Recalculado sempre (não persistido em `ScanResult` — plano §7.2 trata
        resolução de código como algo a confrontar contra o banco, não a
        gravar) e, crucialmente, **sem filtrar pela carta que o motor elegeu
        vencedora do nome**: é isto que a tela de revisão usa para preencher o
        set corretamente mesmo quando a pessoa troca de candidato — nome e
        código podem apontar para cartas diferentes (o conflito é por isso
        que a leitura caiu em revisão manual), e o código continua sendo a
        evidência mais confiável assim que a carta certa é escolhida (achado
        real do usuário: trocar de candidato limpava o set, ou herdava o de
        outra carta via filtro de idioma).
        """
        resolver = PrintResolver(session)
        for result in results:
            prints = resolver.resolve(result.ocr_code_raw).prints if result.ocr_code_raw else []
            result.code_prints = [p.as_dict() for p in prints]

    def _attach_card_names(self, session: Session, results: list[ScanResult]) -> None:
        """Anexa o nome da carta já identificada por `card_id`, quando houver.

        Achado real do usuário: quando o nome lido pelo OCR não rendeu
        candidato nenhum (comum em cartas JP/CJK — o motor de nome nem
        tenta, `matching/candidates.py::MIN_MATCHABLE_LENGTH`) mas o
        passcode ou o código sozinhos já identificaram a carta
        (`_from_passcode`/`_from_code_only`), `ScanResult.candidates` fica
        vazio e a revisão não tinha como sugerir nada — obrigando busca
        manual do zero para uma carta que o sistema já sabia qual era. Uma
        query só para o lote inteiro, mesmo espírito de `_attach_code_prints`.
        """
        card_ids = {result.card_id for result in results if result.card_id is not None}
        names: dict[int, str] = {}
        if card_ids:
            rows = session.execute(select(Card.id, Card.name).where(Card.id.in_(card_ids)))
            names = {row.id: row.name for row in rows}
        for result in results:
            result.card_name = names.get(result.card_id) if result.card_id is not None else None

    def _attach_print_info(self, session: Session, results: list[ScanResult]) -> None:
        """Anexa o set/raridade **resolvidos no catálogo** de `card_print_id`
        (não o texto bruto do OCR) — o comparador da tela de detalhe do scan
        (plano do usuário: conferir visualmente a leitura contra o que foi
        de fato gravado). Uma query só para o lote inteiro, mesmo espírito de
        `_attach_code_prints`/`_attach_card_names`.
        """
        print_ids = {r.card_print_id for r in results if r.card_print_id is not None}
        prints: dict[int, CardPrint] = {}
        if print_ids:
            rows = session.scalars(select(CardPrint).where(CardPrint.id.in_(print_ids)))
            prints = {row.id: row for row in rows}
        for result in results:
            card_print = (
                prints.get(result.card_print_id) if result.card_print_id is not None else None
            )
            result.resolved_set_code = card_print.set_code_full if card_print else None
            result.resolved_rarity = card_print.rarity if card_print else None

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
            self._attach_code_prints(session, [result])
            self._attach_card_names(session, [result])
            session.expunge(result)
            return result

    def confirm_result(
        self,
        result_id: int,
        *,
        card_id: int,
        card_print_id: int | None = None,
        set_code_full: str | None = None,
        rarity_override: str | None = None,
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

        `set_code_full`/`rarity_override` só se aplicam quando `card_print_id`
        é `None`: é o caso "sei o set, a raridade real não está catalogada" —
        a revisão manda o set escolhido no seletor em cascata e o texto livre
        digitado, em vez de forçar um print inventado.
        """
        with self.database.session() as session:
            scan_repo = ScanRepository(session)
            collection_repo = CollectionRepository(session)

            result = scan_repo.get_result(result_id)
            if result is None:
                raise ScanResultNotFoundError(result_id)
            if result.applied:
                raise ScanResultAlreadyAppliedError(result_id)

            # Se a raridade escolhida/digitada bater com uma catalogada para
            # este set, resolve o print de verdade em vez de gravar como
            # pendente — "raridade não catalogada" só quando nenhuma bate.
            if card_print_id is None and set_code_full and rarity_override:
                matches = [
                    p
                    for p in PrintResolver(session).prints_for_card(card_id, set_code_full)
                    if (p.rarity or "").casefold() == rarity_override.casefold()
                ]
                if len(matches) == 1:
                    card_print_id = matches[0].print_id
                    set_code_full = None
                    rarity_override = None

            # `result.detected_language` vem de ler a carta física (nome/código)
            # e continua tendo prioridade — inclusive sobre o print escolhido,
            # porque o fallback de idioma (ADR 0009/0012, `matching/resolver.py`)
            # deliberadamente resolve para um print EM INGLÊS quando a carta é
            # PT/DE/FR/IT mas aquele print específico não tem edição regional
            # catalogada; `card_print.region` mentiria "EN" nesse caso. Só cai
            # para a região do print quando a leitura não capturou idioma
            # nenhum — achado real do usuário: o OCR trocou "PT011" por "1T011"
            # (P→1, erro comum, `domain/setcode.py`), a revisão confirmou
            # manualmente o print certo (region="PT"), mas sem isto a cópia
            # entrava com `language="EN"` e virava uma segunda linha na coleção
            # em vez de somar na cópia já existente do mesmo print.
            print_region = (
                session.scalar(select(CardPrint.region).where(CardPrint.id == card_print_id))
                if card_print_id is not None
                else None
            )
            resolved_language = (
                language or result.detected_language or print_region or DEFAULT_LANGUAGE
            )
            if not (2 <= len(resolved_language) <= 3 and resolved_language.isupper()):
                # Mesmo limiar do CHECK de `card_print_override`
                # (`ck_card_print_override_language_format`). `detected_language`
                # normalmente já vem validado (`PrintResolver.resolve` só confia
                # em região de 1 letra depois de casar no catálogo), mas linhas
                # gravadas antes dessa correção — ou um `language` arbitrário
                # vindo da API — ainda podem carregar lixo. Cair para o padrão
                # aqui é melhor que estourar `IntegrityError` dentro do flush lá
                # embaixo e desfazer a confirmação inteira sem erro nenhum
                # visível na tela (achado real: Scan #11, "P" de "PT" corrompido
                # pelo OCR travava a revisão dessas cartas para sempre).
                resolved_language = DEFAULT_LANGUAGE
            key = CollectionKey(
                card_id=card_id,
                card_print_id=card_print_id,
                set_code_full=set_code_full if card_print_id is None else None,
                rarity=rarity_override if card_print_id is None else None,
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

    def rarities_for_set(self, card_id: int, set_code_full: str) -> list[str]:
        """Raridades catalogadas para essa carta nesse set — usado pela
        revisão (CLI/Web) para oferecer uma picklist em vez de texto livre
        às cegas quando `card_print_id` ficou `None` por ambiguidade."""
        with self.database.session() as session:
            return PrintResolver(session).rarities_for_set(card_id, set_code_full)

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

    def reject_all_pending(self) -> int:
        """Rejeita toda a fila de revisão de uma vez. Devolve quantos itens
        foram removidos da fila, para a UI confirmar o que aconteceu."""
        with self.database.session() as session:
            return ScanRepository(session).reject_all_pending()

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

    def delete_job(self, job_id: int) -> None:
        """Apaga um job de scan e suas fotos/leituras (pedido do usuário:
        "limpar scans", plano §10.1/§10.2). Nunca toca na coleção — ver
        `ScanRepository.delete_job`."""
        with self.database.session() as session:
            scan_repo = ScanRepository(session)
            job = scan_repo.get_job(job_id)
            if job is None:
                raise JobNotFoundError(job_id)
            scan_repo.delete_job(job)

    def clear_all_jobs(self) -> int:
        """Apaga todos os jobs de scan de uma vez. Devolve quantos foram
        removidos, para a UI confirmar o que aconteceu."""
        with self.database.session() as session:
            return ScanRepository(session).clear_all_jobs()

    def job_results(self, job_id: int) -> list[ScanResult]:
        """Os resultados de um job (Fase 8: `GET /scan/{id}` e `/api/v1/scans/{id}/results`)."""
        with self.database.session() as session:
            if ScanRepository(session).get_job(job_id) is None:
                raise JobNotFoundError(job_id)
            results = ScanRepository(session).results_for_job(job_id)
            for result in results:
                _ = result.scan_image.file_path
            # Comparador da tela de detalhe do scan (pedido do usuário): nome
            # e set/raridade resolvidos, ao lado do texto bruto do OCR.
            self._attach_card_names(session, results)
            self._attach_print_info(session, results)
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
        require_set: bool | None = None,
        require_rarity: bool | None = None,
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
            # Mesma resolução de `None` que `_handle_crop` aplica por recorte
            # (settings.auto_requires_print/rarity como default do override
            # por scan) — calculada uma vez aqui só para congelar no job
            # qual política valeu para esta execução (ver comentário em
            # `ScanJob.auto_enabled` sobre por quê).
            effective_require_set = (
                self.settings.auto_requires_print if require_set is None else require_set
            )
            effective_require_rarity = (
                self.settings.auto_requires_rarity if require_rarity is None else require_rarity
            )
            job = scan_repo.create_job(
                folder_path=str(report.folder),
                ocr_provider=provider_name or self.settings.ocr_provider,
                workers=workers or self.settings.effective_workers,
                auto_enabled=not no_auto,
                require_set=effective_require_set,
                require_rarity=effective_require_rarity,
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
                    require_set=require_set,
                    require_rarity=require_rarity,
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
        require_set: bool | None = None,
        require_rarity: bool | None = None,
    ) -> list[ImageEvent]:
        """Bookkeeping por foto; o matching/decisão de cada recorte é
        delegado a `_handle_crop` — uma foto sem grade rende exatamente um
        recorte (`CropRegion(0, 1, None)`), então o caminho de hoje (uma
        foto = uma carta) produz exatamente um `ImageEvent`, como sempre.
        """
        report.processed += 1
        report.skipped_empty += outcome.skipped_empty

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
                job_id=job_id,
                engine=engine,
                report=report,
                apply=apply,
                no_auto=no_auto,
                require_set=require_set,
                require_rarity=require_rarity,
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
        job_id: int | None,
        engine: MatchingEngine,
        report: ScanRunReport,
        apply: bool,
        no_auto: bool,
        scan_repo: ScanRepository,
        collection_repo: CollectionRepository,
        require_set: bool | None = None,
        require_rarity: bool | None = None,
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

        match_result = engine.match(
            crop.read_name, crop.read_code or None, crop.read_passcode or None
        )
        decision = match_result.decision
        ocr_name_raw = crop.read_name
        ocr_code_raw = crop.read_code
        ocr_passcode_raw = crop.read_passcode

        # Cascata (plano §6.3): só a fração duvidosa reprocessa por LLM —
        # decisão `auto` já está resolvida e não vale o custo/latência extra.
        if decision != Decision.AUTO:
            fallback = self._try_fallback(outcome.path, crop.region, engine)
            if fallback is not None and fallback.match.confidence >= match_result.confidence:
                match_result = fallback.match
                decision = fallback.match.decision
                # O texto bruto persistido precisa ser o que de fato produziu
                # esta decisão — senão a revisão mostra "(vazio)" ao lado de
                # uma carta identificada (achado real: o worker local não leu
                # nada, o fallback leu e casou, mas `ScanResult.ocr_name_raw`
                # continuava com o texto do worker).
                ocr_name_raw = fallback.name_raw or ocr_name_raw
                ocr_code_raw = fallback.code_raw or ocr_code_raw
                ocr_passcode_raw = fallback.passcode_raw or ocr_passcode_raw

        if no_auto and decision == Decision.AUTO:
            decision = Decision.PENDING

        # Política "AUTO exige SET"/"AUTO exige RARIDADE" — duas exigências
        # independentes, nunca a mesma coisa (pedido do usuário): `set_code`
        # sozinho já significa "identifiquei o set/print corretamente", ainda
        # que 2+ raridades catalogadas para ele deixem `card_print_id` em
        # `None` até alguém escolher qual. Sem separar as duas, uma leitura
        # com set 100% certo e só a raridade pendente caía em revisão do
        # mesmo jeito que uma leitura sem set nenhum — a diferença importa
        # porque o set já é o suficiente para "a carta foi analisada com
        # sucesso" (o item entra com `CollectionItem.set_code_full`
        # preenchido e raridade pendente, resolvível depois em `/collection`).
        if decision == Decision.AUTO and match_result.card_print_id is None:
            requires_set = (
                self.settings.auto_requires_print if require_set is None else require_set
            )
            requires_rarity = (
                self.settings.auto_requires_rarity
                if require_rarity is None
                else require_rarity
            )
            set_known = match_result.set_code is not None
            if not set_known and requires_set:
                decision = Decision.PENDING
                match_result.reason = (
                    "carta identificada, mas sem set/print resolvido — confirme manualmente"
                )
            elif set_known and requires_rarity:
                decision = Decision.PENDING
                match_result.reason = (
                    "set identificado, mas raridade ambígua — confirme manualmente"
                )

        applied = False
        result_id: int | None = None
        if apply and image is not None:
            applied, result_id = self._apply_decision(
                image.id,
                decision,
                match_result,
                job_id=job_id,
                ocr_name_raw=ocr_name_raw or None,
                ocr_code_raw=ocr_code_raw or None,
                ocr_passcode_raw=ocr_passcode_raw or None,
                scan_repo=scan_repo,
                collection_repo=collection_repo,
                crop_index=crop.region.index,
                crop_count=crop.region.count,
                source_bbox=crop.region.bbox,
            )

        # `auto_added`/`pending` espelham a decisão do motor (`decision`),
        # igual ao que cada `ImageEvent` individual mostra — achado real do
        # usuário (Scan #70): contar por `applied` fazia o resumo do job
        # mentir feio num `--reprocess`, jogando recortes já resolvidos
        # (`has_applied_result`) para dentro de `pending` mesmo quando o
        # motor decidiu `auto` e o log por imagem dizia `[auto]`. `pending`
        # tem que significar "precisa de revisão" — um recorte já aplicado
        # antes não precisa, `already_applied` é onde essa informação mora.
        if decision == Decision.AUTO:
            report.auto_added += 1
            if apply and not applied:
                report.already_applied += 1
        else:
            report.pending += 1

        return ImageEvent(
            file_name=outcome.path.name,
            status=crop.status,
            decision=decision.value,
            result_id=result_id,
            ocr_name=ocr_name_raw or None,
            ocr_code=ocr_code_raw or None,
            ocr_passcode=ocr_passcode_raw or None,
            card_name=match_result.card_name,
            set_code=match_result.set_code,
            confidence=match_result.confidence,
            passcode_verified=match_result.passcode_verified,
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
        job_id: int | None,
        ocr_name_raw: str | None,
        ocr_code_raw: str | None,
        ocr_passcode_raw: str | None = None,
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
            job_id=job_id,
            card_id=match_result.card_id,
            card_print_id=match_result.card_print_id,
            # O texto **bruto** do OCR — não o nome/código já resolvidos no
            # catálogo. É o que a tela de revisão (Fase 6/8) precisa mostrar
            # ao lado da carta identificada para o usuário julgar a leitura.
            ocr_name_raw=ocr_name_raw,
            ocr_code_raw=ocr_code_raw,
            ocr_passcode_raw=ocr_passcode_raw,
            name_score=match_result.name_score,
            code_score=match_result.code_score,
            confidence=match_result.confidence,
            margin=match_result.margin,
            candidates=match_result.candidates or None,
            decision=decision.value,
            detected_language=match_result.matched_language,
            matched_set_code=match_result.set_code,
            passcode_verified=match_result.passcode_verified,
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
            # Só relevante com `--allow-no-set-auto`: a política padrão
            # (`auto_requires_print`) já rebaixa para PENDING antes daqui
            # quando `print_id is None`, mas se o usuário optou por permitir
            # mesmo assim, o set identificado não pode se perder.
            set_code_full=match_result.set_code if print_id is None else None,
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
    ) -> FallbackReading | None:
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
        passcode = result.best(REGION_PASSCODE)
        fallback_match = engine.match(name, code or None, passcode or None)
        log.info(
            "scan.fallback_used",
            file=path.name,
            crop_index=region.index,
            confidence=round(fallback_match.confidence, 3),
        )
        return FallbackReading(
            match=fallback_match, name_raw=name, code_raw=code, passcode_raw=passcode
        )
