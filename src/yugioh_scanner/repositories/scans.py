"""Repositório de jobs, imagens e resultados de scan (plano §13).

A idempotência inteira do scanner depende de duas garantias que este módulo
mantém:

1. `ScanImage.file_hash` é **globalmente** único (índice do banco, não deste
   código) — a mesma foto nunca ganha uma segunda linha, mesmo renomeada, mesmo
   em outro job.
2. Uma `ScanImage` só pode **aplicar** na coleção uma vez. `--reprocess` cria um
   novo `ScanResult` (nova opinião sobre a mesma foto) mas nunca soma outra
   cópia à coleção — isso exigiria `--reapply` explícito (fora do escopo desta
   fase; ver plano §13.3).
"""

from __future__ import annotations

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from ..db.tables import CollectionItem, ScanImage, ScanJob, ScanResult, utcnow
from ..images.preprocess import BoundingBox


class ScanRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    # --------------------------------------------------------------------- job

    def create_job(
        self,
        folder_path: str,
        ocr_provider: str,
        workers: int,
        *,
        auto_enabled: bool = True,
        require_set: bool = True,
        require_rarity: bool = False,
    ) -> ScanJob:
        job = ScanJob(
            folder_path=folder_path,
            ocr_provider=ocr_provider,
            workers=workers,
            status="running",
            auto_enabled=auto_enabled,
            require_set=require_set,
            require_rarity=require_rarity,
        )
        self.session.add(job)
        self.session.flush()
        return job

    def finish_job(
        self,
        job: ScanJob,
        *,
        status: str = "done",
        error: str | None = None,
        total_images: int | None = None,
        processed: int | None = None,
        skipped: int | None = None,
        auto_added: int | None = None,
        pending: int | None = None,
        failed: int | None = None,
    ) -> None:
        """Fecha o job — e é aqui que as contagens finais ficam persistidas.

        Regressão real (Fase 6): as colunas de contagem existiam no schema
        desde a Fase 1, mas nada nunca as escrevia — só o `ScanRunReport`
        em memória (devolvido para quem chamou `scan()`) carregava os
        números certos. `scan-status`/`recent_jobs()` liam sempre zero até
        um comando novo (que consulta o `ScanJob` persistido em vez do
        report de uma execução específica) expor o buraco.
        """
        job.status = status
        job.error = error
        job.finished_at = utcnow()
        if total_images is not None:
            job.total_images = total_images
        if processed is not None:
            job.processed = processed
        if skipped is not None:
            job.skipped = skipped
        if auto_added is not None:
            job.auto_added = auto_added
        if pending is not None:
            job.pending = pending
        if failed is not None:
            job.failed = failed

    def get_job(self, job_id: int) -> ScanJob | None:
        return self.session.get(ScanJob, job_id)

    def results_for_job(self, job_id: int) -> list[ScanResult]:
        """Todos os resultados de um job — a grade de `/scan/{id}` (Fase 8).

        Filtra por `ScanResult.job_id` (o job que **produziu** a leitura),
        não por `scan_image.job_id` (o job que descobriu o arquivo pela
        primeira vez) — achado real do usuário: um `--reprocess` cria
        `ScanResult` novos para arquivos já conhecidos de um job anterior;
        filtrar pelo dono original da `ScanImage` fazia esses resultados
        nunca aparecerem na tela do job que de fato os gerou.
        """
        stmt = (
            select(ScanResult).where(ScanResult.job_id == job_id).order_by(ScanResult.id)
        )
        return list(self.session.scalars(stmt))

    def delete_job(self, job: ScanJob) -> None:
        """Apaga um job e, por cascata de FK (`scan_image`/`scan_result`
        apontam para `scan_job.id` com `ON DELETE CASCADE`), todas as fotos e
        leituras associadas a ele. `CollectionItem` não é tocado — o vínculo
        é só `ScanResult.collection_item_id`, e é a `CollectionItem` que
        aponta pra fora (`ON DELETE SET NULL`), nunca o contrário; limpar
        histórico de scan nunca remove carta já somada à coleção."""
        self.session.delete(job)

    def clear_all_jobs(self) -> int:
        """Apaga TODOS os jobs de scan (pedido do usuário: "limpar tudo").
        Mesma garantia acima sobre a coleção, e mesmo motivo de usar `DELETE`
        em massa em vez de um loop (`CollectionRepository.clear_all`)."""
        count = len(self.session.scalars(select(ScanJob.id)).all())
        self.session.execute(delete(ScanJob))
        self.session.flush()
        return count

    def recent_jobs(self, limit: int = 10) -> list[ScanJob]:
        # `id` como desempate: dois jobs criados na mesma janela de resolução
        # do timestamp (comum em testes, e possível em uso real com CLI/Web
        # disparando scans em sequência rápida) empatam em `started_at`, e o
        # SQLite não garante que o empate resolva por ordem de inserção.
        stmt = select(ScanJob).order_by(ScanJob.started_at.desc(), ScanJob.id.desc()).limit(limit)
        return list(self.session.scalars(stmt))

    # ------------------------------------------------------------------- imagem

    def find_by_hash(self, file_hash: str) -> ScanImage | None:
        """A checagem central da idempotência: esta foto já foi vista?"""
        return self.session.scalars(
            select(ScanImage).where(ScanImage.file_hash == file_hash)
        ).first()

    def known_hashes(self, hashes: list[str]) -> set[str]:
        """De um lote de hashes, quais já existem no banco.

        Uma consulta só para a pasta inteira — evita N idas ao banco antes de
        sequer começar o OCR (plano §20: evitar chamadas desnecessárias).
        """
        if not hashes:
            return set()
        rows = self.session.scalars(
            select(ScanImage.file_hash).where(ScanImage.file_hash.in_(hashes))
        )
        return set(rows)

    def create_image(
        self,
        job_id: int,
        *,
        file_path: str,
        file_hash: str,
        file_size: int,
        status: str,
        error: str | None = None,
        ocr_raw: dict | None = None,
        ocr_ms: int | None = None,
    ) -> ScanImage:
        image = ScanImage(
            job_id=job_id,
            file_path=file_path,
            file_hash=file_hash,
            file_size=file_size,
            status=status,
            error=error,
            ocr_raw=ocr_raw,
            ocr_ms=ocr_ms,
        )
        self.session.add(image)
        self.session.flush()
        return image

    def update_image_reading(
        self,
        image: ScanImage,
        *,
        status: str,
        ocr_raw: dict | None,
        ocr_ms: int | None,
        error: str | None = None,
    ) -> None:
        """Atualiza a leitura de OCR de uma imagem já conhecida (`--reprocess`).

        A `ScanImage` guarda a leitura **mais recente**; o histórico de
        decisões fica em `ScanResult`, que é o que ganha uma linha nova.
        """
        image.status = status
        image.ocr_raw = ocr_raw
        image.ocr_ms = ocr_ms
        image.error = error

    # ------------------------------------------------------------------ resultado

    def has_applied_result(self, scan_image_id: int, *, crop_index: int = 0) -> bool:
        """Este recorte já contribuiu para a coleção em alguma execução anterior?

        É a guarda que faz `--reprocess` nunca duplicar quantidade: mesmo que a
        nova leitura dê `AUTO`, não aplicamos de novo se uma leitura anterior já
        aplicou (plano §13.2 — "uma `scan_image` contribui no máximo uma vez").

        Filtrado por `crop_index` porque, numa foto com grade (plano §22), cada
        recorte é uma carta diferente e aplica independentemente — sem o
        filtro, uma foto de 4 cartas com o recorte #0 já aplicado faria os
        recortes #1-#3 serem recusados por engano num `--reprocess`.

        O `JOIN` contra `CollectionItem` (não só `ScanResult.applied`) é
        deliberado: achado real do usuário — a coleção ficou vazia por fora
        deste caminho (reset de banco durante um teste), mas os `ScanResult`
        antigos continuavam com `applied=True`, e a guarda recusava reaplicar
        para sempre, mesmo a carta não existindo mais na coleção. `applied`
        sozinho é só "alguém marcou uma vez"; o que importa é se aquele
        `collection_item` ainda existe de verdade.
        """
        return (
            self.session.scalars(
                select(ScanResult.id)
                .join(CollectionItem, CollectionItem.id == ScanResult.collection_item_id)
                .where(
                    ScanResult.scan_image_id == scan_image_id,
                    ScanResult.crop_index == crop_index,
                    ScanResult.applied.is_(True),
                )
            ).first()
            is not None
        )

    def create_result(
        self,
        scan_image_id: int,
        *,
        job_id: int | None,
        card_id: int | None,
        card_print_id: int | None,
        ocr_name_raw: str | None,
        ocr_code_raw: str | None,
        name_score: float,
        code_score: float,
        confidence: float,
        margin: float,
        candidates: list[dict] | None,
        decision: str,
        detected_language: str | None = None,
        matched_set_code: str | None = None,
        ocr_passcode_raw: str | None = None,
        passcode_verified: bool = False,
        crop_index: int = 0,
        crop_count: int = 1,
        source_bbox: BoundingBox | None = None,
    ) -> ScanResult:
        result = ScanResult(
            scan_image_id=scan_image_id,
            job_id=job_id,
            card_id=card_id,
            card_print_id=card_print_id,
            ocr_name_raw=ocr_name_raw,
            ocr_code_raw=ocr_code_raw,
            matched_set_code=matched_set_code,
            ocr_passcode_raw=ocr_passcode_raw,
            passcode_verified=passcode_verified,
            name_score=name_score,
            code_score=code_score,
            confidence=confidence,
            margin=margin,
            candidates=candidates,
            decision=decision,
            detected_language=detected_language,
            crop_index=crop_index,
            crop_count=crop_count,
            source_bbox_left=source_bbox.left if source_bbox is not None else None,
            source_bbox_top=source_bbox.top if source_bbox is not None else None,
            source_bbox_right=source_bbox.right if source_bbox is not None else None,
            source_bbox_bottom=source_bbox.bottom if source_bbox is not None else None,
        )
        self.session.add(result)
        self.session.flush()
        return result

    def mark_applied(self, result: ScanResult, collection_item_id: int) -> None:
        result.applied = True
        result.collection_item_id = collection_item_id
        result.decided_at = utcnow()
        # A sessão é `autoflush=False` (plano §20.1 — evitar flushes implícitos
        # no meio de um lote): sem este flush explícito, uma consulta seguinte
        # no mesmo scan (ex.: `has_applied_result` de uma próxima imagem) não
        # enxergaria esta mudança até o próximo commit/flush manual.
        self.session.flush()

    def mark_decided(self, result: ScanResult, decision: str | None = None) -> None:
        """Marca uma decisão explícita sem aplicar (confirmação/rejeição manual)."""
        if decision is not None:
            result.decision = decision
        result.decided_at = utcnow()
        self.session.flush()

    def get_result(self, result_id: int) -> ScanResult | None:
        return self.session.get(ScanResult, result_id)

    #: Decisões que ainda esperam um humano. Inclui "unmatched" de propósito:
    #: é o caso com zero candidatos — o que mais precisa de olho humano, não
    #: menos. (Achado por teste: `no_auto=True` sobre uma leitura sem nenhum
    #: candidato produz "unmatched", não "pending"; excluí-la da fila deixaria
    #: essas leituras presas para sempre, sem forma de revisar ou rejeitar.)
    _REVIEWABLE_DECISIONS = ("pending", "manual", "unmatched")

    def pending_results(self, limit: int = 100) -> list[ScanResult]:
        """Fila de revisão: decisões que ainda esperam um humano."""
        stmt = (
            select(ScanResult)
            .where(ScanResult.decision.in_(self._REVIEWABLE_DECISIONS))
            .where(ScanResult.applied.is_(False))
            .order_by(ScanResult.created_at)
            .limit(limit)
        )
        return list(self.session.scalars(stmt))

    def count_pending(self) -> int:
        return len(
            self.session.scalars(
                select(ScanResult.id)
                .where(ScanResult.decision.in_(self._REVIEWABLE_DECISIONS))
                .where(ScanResult.applied.is_(False))
            ).all()
        )

    def reject_all_pending(self) -> int:
        """Rejeita TODA a fila de revisão de uma vez (pedido do usuário:
        "limpar todas as revisões"). Mesma semântica de `mark_decided` por
        item (nunca toca a coleção, `ScanImage` continua marcada como
        vista), só que em massa — `UPDATE` direto em vez de um loop, mesmo
        motivo de `ScanRepository.clear_all_jobs`."""
        ids = self.session.scalars(
            select(ScanResult.id)
            .where(ScanResult.decision.in_(self._REVIEWABLE_DECISIONS))
            .where(ScanResult.applied.is_(False))
        ).all()
        if not ids:
            return 0
        self.session.execute(
            update(ScanResult)
            .where(ScanResult.id.in_(ids))
            .values(decision="rejected", decided_at=utcnow())
        )
        self.session.flush()
        return len(ids)
