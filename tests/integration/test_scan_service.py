"""ScanService: o ciclo scan → coleção com idempotência real (Fase 5).

Usa `FakeOCRProvider` roteirizado por nome de arquivo contra o catálogo real
das fixtures — determinístico, sem OCR de verdade, focado na lógica de
coleção/idempotência que é o que esta fase entrega (a qualidade do OCR em si
já tem suíte própria nas Fases 3/4).

`workers=1` em todo teste: força `SerialScanExecutor`, que roda no mesmo
processo. Um `FakeOCRProvider` registrado por closure não sobreviveria ao
`spawn` de um pool de processos de verdade.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tests.factories import make_card_image, make_corrupted_image
from yugioh_scanner.config import Settings
from yugioh_scanner.db.session import Database
from yugioh_scanner.db.tables import CardPrint, CollectionItem, ScanImage, ScanJob, ScanResult
from yugioh_scanner.ocr.fake_provider import FakeOCRProvider
from yugioh_scanner.ocr.registry import register_provider, unregister_provider
from yugioh_scanner.scanner.worker import reset_provider
from yugioh_scanner.services.scan_service import ScanService

BLUE_EYES = 89631139
DARK_MAGICIAN = 46986414
POT_OF_GREED = 55144522

#: Roteiro: nome do arquivo → o que o "OCR" leu. `CT13-EN008` e `SDK-001` são
#: prints únicos na fixture (sem ambiguidade de raridade, diferente de
#: `LOB-001`) — a intenção é exercitar coleção/idempotência, não desempate.
SCRIPT = {
    "blue_eyes.jpg": {"name": "Blue-Eyes White Dragon", "code": "CT13-EN008"},
    "dark_magician.jpg": {"name": "Dark Magician", "code": "SDK-001"},
    "pot_of_greed.jpg": {"name": "Pot of Greed"},  # sem código: entra com print NULL
    "garbage.jpg": {"name": "ZZQX WROMBAT FLURB"},  # não casa com nada
}


@pytest.fixture(autouse=True)
def _fake_provider() -> Iterator[None]:
    register_provider("fake", lambda _settings: FakeOCRProvider(SCRIPT))
    try:
        yield
    finally:
        unregister_provider("fake")
        reset_provider()


@pytest.fixture
def cards_folder(tmp_path: Path) -> Path:
    """Uma imagem distinta por arquivo — hashes iguais fariam o sistema
    tratá-las (corretamente) como a mesma foto, o que não é o que estes
    testes querem exercitar."""
    folder = tmp_path / "cards"
    for filename, reading in SCRIPT.items():
        make_card_image(
            folder / filename,
            name=reading.get("name", "X"),
            set_code=reading.get("code") or "LOB-001",
        )
    return folder


def service(catalog: Database, settings: Settings) -> ScanService:
    return ScanService(catalog, settings)


def scan(catalog: Database, settings: Settings, folder: Path, **kwargs: object) -> object:
    kwargs.setdefault("provider_name", "fake")
    kwargs.setdefault("workers", 1)
    return service(catalog, settings).scan(folder, **kwargs)  # type: ignore[arg-type]


def snapshot(catalog: Database) -> list[tuple[int, int | None, int]]:
    """Retrato da coleção: (card_id, print_id, quantidade), ordenado."""
    with catalog.session() as session:
        rows = session.execute(
            select(CollectionItem.card_id, CollectionItem.card_print_id, CollectionItem.quantity)
        ).all()
        return sorted(tuple(row) for row in rows)  # type: ignore[misc]


class TestAutomaticMatching:
    """Critério de aceitação: leituras boas entram sozinhas, o resto pendura."""

    def test_high_confidence_matches_are_applied(
        self, catalog: Database, settings: Settings, cards_folder: Path
    ) -> None:
        report = scan(catalog, settings, cards_folder)
        assert report.total_images == 4
        assert report.auto_added == 3
        assert report.pending == 1  # "garbage.jpg", sem candidato
        assert report.failed == 0

    def test_applied_items_land_in_the_collection(
        self, catalog: Database, settings: Settings, cards_folder: Path
    ) -> None:
        scan(catalog, settings, cards_folder)
        items = snapshot(catalog)
        assert (BLUE_EYES, None, 1) not in items  # tinha código: não fica NULL
        assert (DARK_MAGICIAN, None, 1) not in items
        assert (POT_OF_GREED, None, 1) in items  # sem código: fica NULL mesmo

    def test_card_without_code_gets_null_print(
        self, catalog: Database, settings: Settings, cards_folder: Path
    ) -> None:
        """Critério de aceitação explícito da Fase 5."""
        scan(catalog, settings, cards_folder)
        with catalog.session() as session:
            item = session.execute(
                select(CollectionItem).where(CollectionItem.card_id == POT_OF_GREED)
            ).scalar_one()
            assert item.card_print_id is None

    def test_unmatched_reading_is_not_in_the_collection(
        self, catalog: Database, settings: Settings, cards_folder: Path
    ) -> None:
        """`garbage.jpg` não casa com nada: 4 imagens, só 3 viram item."""
        scan(catalog, settings, cards_folder)
        with catalog.session() as session:
            assert session.scalar(select(func.count()).select_from(CollectionItem)) == 3

    def test_scan_result_stores_the_raw_ocr_text_not_the_resolved_name(
        self, catalog: Database, settings: Settings, cards_folder: Path
    ) -> None:
        """Regressão: `ocr_name_raw` precisa ser o que o OCR leu, não a carta
        resolvida. Uma leitura sem candidato (`garbage.jpg`) prova a diferença:
        o texto bruto sobrevive mesmo quando nada casa com o catálogo."""
        scan(catalog, settings, cards_folder)
        with catalog.session() as session:
            image = session.execute(
                select(ScanImage).where(ScanImage.file_path.like("%garbage.jpg"))
            ).scalar_one()
            result = session.execute(
                select(ScanResult).where(ScanResult.scan_image_id == image.id)
            ).scalar_one()
            assert result.ocr_name_raw == "ZZQX WROMBAT FLURB"
            assert result.card_id is None

    def test_job_and_result_rows_are_persisted(
        self, catalog: Database, settings: Settings, cards_folder: Path
    ) -> None:
        report = scan(catalog, settings, cards_folder)
        with catalog.session() as session:
            job = session.get(ScanJob, report.job_id)
            assert job is not None
            assert job.status == "done"
            assert session.scalar(select(func.count()).select_from(ScanImage)) == 4
            assert session.scalar(select(func.count()).select_from(ScanResult)) == 4

    def test_job_counters_are_persisted_not_just_in_the_report(
        self, catalog: Database, settings: Settings, cards_folder: Path
    ) -> None:
        """Regressão: as colunas de contagem do `ScanJob` existem no schema
        desde a Fase 1 mas nunca eram escritas — só o `ScanRunReport` em
        memória (devolvido só para esta chamada) tinha os números certos.
        Um `scan-status` lendo o job persistido via outra sessão via sempre
        zero."""
        report = scan(catalog, settings, cards_folder)
        with catalog.session() as session:
            job = session.get(ScanJob, report.job_id)
            assert job is not None
            assert job.total_images == report.total_images == 4
            assert job.processed == report.processed == 4
            assert job.auto_added == report.auto_added == 3
            assert job.pending == report.pending == 1
            assert job.failed == report.failed == 0
            assert job.skipped == report.skipped == 0


class _StubLanguageResolver:
    """Sempre devolve o mesmo print, não importa quem pergunta — usado só
    para provar que `ScanService` de fato consulta o resolver injetado e usa
    o que ele devolve, sem depender de o catálogo de fixtures ter um par
    EN/PT de verdade (só produtos "OP" têm isso — ver docs/adr/0009)."""

    def __init__(self, print_id: int) -> None:
        self.print_id = print_id
        self.calls: list[tuple[int, str]] = []

    def resolve(self, session: Session, card_id: int, current_print: object, language: str) -> CardPrint | None:
        self.calls.append((card_id, language))
        return session.get(CardPrint, self.print_id)


class TestDetectedLanguage:
    """Continuação do plano de idiomas (docs/adr/0009): o idioma do candidato
    de nome vencedor vira `ScanResult.detected_language`, e o caminho
    automático já registra esse idioma na coleção — sem precisar de revisão
    humana para isso."""

    def test_pt_alt_name_match_is_recorded_and_tags_the_collection_item(
        self, catalog: Database, settings: Settings, tmp_path: Path
    ) -> None:
        folder = tmp_path / "cards_lang"
        make_card_image(folder / "mago_negro.jpg", name="Mago Negro")
        register_provider(
            "fake", lambda _settings: FakeOCRProvider({"mago_negro.jpg": {"name": "Mago Negro"}})
        )
        reset_provider()

        report = scan(catalog, settings, folder)
        assert report.auto_added == 1

        with catalog.session() as session:
            result = session.execute(select(ScanResult)).scalar_one()
            assert result.card_id == DARK_MAGICIAN
            assert result.detected_language == "PT"

            item = session.execute(
                select(CollectionItem).where(CollectionItem.card_id == DARK_MAGICIAN)
            ).scalar_one()
            assert item.language == "PT"

    def test_english_match_is_recorded_as_en(
        self, catalog: Database, settings: Settings, cards_folder: Path
    ) -> None:
        scan(catalog, settings, cards_folder)
        with catalog.session() as session:
            image = session.execute(
                select(ScanImage).where(ScanImage.file_path.like("%blue_eyes.jpg"))
            ).scalar_one()
            result = session.execute(
                select(ScanResult).where(ScanResult.scan_image_id == image.id)
            ).scalar_one()
            assert result.detected_language == "EN"

            item = session.execute(
                select(CollectionItem).where(CollectionItem.card_id == BLUE_EYES)
            ).scalar_one()
            assert item.language == "EN"

    def test_auto_path_uses_the_injected_language_resolver_to_swap_the_print(
        self, catalog: Database, settings: Settings, tmp_path: Path
    ) -> None:
        """A troca de print no caminho automático passa pelo
        `PrintLanguageResolver` injetado (plano de idiomas, docs/adr/0009) —
        e o registro de auditoria (`ScanResult.card_print_id`) acompanha."""
        folder = tmp_path / "cards_lang"
        make_card_image(folder / "mago_negro.jpg", name="Mago Negro")
        register_provider(
            "fake", lambda _settings: FakeOCRProvider({"mago_negro.jpg": {"name": "Mago Negro"}})
        )
        reset_provider()

        with catalog.session() as session:
            sdk_print_id = session.execute(
                select(CardPrint.id).where(
                    CardPrint.card_id == DARK_MAGICIAN, CardPrint.set_code_full == "SDK-001"
                )
            ).scalar_one()

        stub = _StubLanguageResolver(sdk_print_id)
        report = ScanService(catalog, settings, language_resolver=stub).scan(
            folder, provider_name="fake", workers=1
        )
        assert report.auto_added == 1
        assert stub.calls == [(DARK_MAGICIAN, "PT")]

        with catalog.session() as session:
            item = session.execute(
                select(CollectionItem).where(CollectionItem.card_id == DARK_MAGICIAN)
            ).scalar_one()
            assert item.card_print_id == sdk_print_id

            result = session.execute(select(ScanResult)).scalar_one()
            assert result.card_print_id == sdk_print_id


class TestIdempotency:
    """O critério central da fase: rodar duas vezes não muda nada."""

    def test_second_run_changes_no_quantities(
        self, catalog: Database, settings: Settings, cards_folder: Path
    ) -> None:
        scan(catalog, settings, cards_folder)
        before = snapshot(catalog)

        report2 = scan(catalog, settings, cards_folder)

        assert snapshot(catalog) == before
        assert report2.skipped == 4
        assert report2.processed == 0
        assert report2.auto_added == 0

    def test_second_run_creates_no_new_rows(
        self, catalog: Database, settings: Settings, cards_folder: Path
    ) -> None:
        scan(catalog, settings, cards_folder)
        with catalog.session() as session:
            images_before = session.scalar(select(func.count()).select_from(ScanImage))
            results_before = session.scalar(select(func.count()).select_from(ScanResult))

        scan(catalog, settings, cards_folder)
        with catalog.session() as session:
            assert session.scalar(select(func.count()).select_from(ScanImage)) == images_before
            assert session.scalar(select(func.count()).select_from(ScanResult)) == results_before

    def test_renaming_the_file_does_not_duplicate(
        self, catalog: Database, settings: Settings, cards_folder: Path
    ) -> None:
        """Identidade por conteúdo, não por caminho (plano §13.1)."""
        # Adiciona um script para o novo nome — o CONTEÚDO é que decide.
        SCRIPT["blue_eyes_renomeada.jpg"] = SCRIPT["blue_eyes.jpg"]
        try:
            scan(catalog, settings, cards_folder)
            before = snapshot(catalog)

            original = cards_folder / "blue_eyes.jpg"
            renamed = cards_folder / "blue_eyes_renomeada.jpg"
            data = original.read_bytes()
            original.unlink()
            renamed.write_bytes(data)  # mesmo conteúdo, nome novo

            report = scan(catalog, settings, cards_folder)
            assert report.skipped == 4  # inclusive a "renomeada": mesmo hash
            assert snapshot(catalog) == before
        finally:
            del SCRIPT["blue_eyes_renomeada.jpg"]

    def test_reprocess_creates_a_new_result_but_does_not_reapply(
        self, catalog: Database, settings: Settings, cards_folder: Path
    ) -> None:
        """Plano §13.3: `--reprocess` re-lê, mas só `--reapply` (fora de
        escopo) poderia somar a mesma foto de novo na coleção."""
        scan(catalog, settings, cards_folder)
        before = snapshot(catalog)
        with catalog.session() as session:
            results_before = session.scalar(select(func.count()).select_from(ScanResult))

        report = scan(catalog, settings, cards_folder, reprocess=True)

        assert report.processed == 4  # reprocessou todas, nenhuma pulada
        assert report.skipped == 0
        assert snapshot(catalog) == before  # nenhuma quantidade mudou

        blue_eyes_event = next(e for e in report.events if e.file_name == "blue_eyes.jpg")
        assert blue_eyes_event.decision == "auto"  # a leitura continua boa…
        assert blue_eyes_event.applied is False  # …mas não foi reaplicada

        with catalog.session() as session:
            results_after = session.scalar(select(func.count()).select_from(ScanResult))
            assert results_after == results_before + 4  # 4 ScanResult novos


class TestDryRun:
    def test_writes_nothing_to_the_database(
        self, catalog: Database, settings: Settings, cards_folder: Path
    ) -> None:
        report = scan(catalog, settings, cards_folder, apply=False)

        assert report.job_id is None
        assert report.auto_added == 3  # a decisão é calculada normalmente…

        with catalog.session() as session:
            # …só não é persistida.
            assert session.scalar(select(func.count()).select_from(ScanJob)) == 0
            assert session.scalar(select(func.count()).select_from(ScanImage)) == 0
            assert session.scalar(select(func.count()).select_from(ScanResult)) == 0
            assert session.scalar(select(func.count()).select_from(CollectionItem)) == 0

    def test_auto_added_reflects_the_decision_not_the_write(
        self, catalog: Database, settings: Settings, cards_folder: Path
    ) -> None:
        """Regressão: `applied` é sempre False em dry-run (nada é gravado).

        Contar `auto_added` por `applied` fazia o preview mentir, mostrando 0
        adições mesmo com leituras que decidiriam `auto`. O preview precisa
        refletir a decisão, não a gravação que nunca acontece aqui.
        """
        report = scan(catalog, settings, cards_folder, apply=False)
        assert report.auto_added == 3
        auto_events = [e for e in report.events if e.decision == "auto"]
        assert len(auto_events) == 3
        assert all(not event.applied for event in auto_events)  # nada é gravado

    def test_dry_run_still_respects_previously_applied_scans(
        self, catalog: Database, settings: Settings, cards_folder: Path
    ) -> None:
        """Um preview precisa ser honesto: já escaneado não aparece de novo."""
        scan(catalog, settings, cards_folder)
        report = scan(catalog, settings, cards_folder, apply=False)
        assert report.skipped == 4
        assert report.processed == 0


class TestNoAuto:
    def test_forces_everything_to_pending(
        self, catalog: Database, settings: Settings, cards_folder: Path
    ) -> None:
        report = scan(catalog, settings, cards_folder, no_auto=True)
        assert report.auto_added == 0
        assert report.pending == 4
        with catalog.session() as session:
            assert session.scalar(select(func.count()).select_from(CollectionItem)) == 0

    def test_decisions_are_downgraded_not_hidden(
        self, catalog: Database, settings: Settings, cards_folder: Path
    ) -> None:
        report = scan(catalog, settings, cards_folder, no_auto=True)
        blue_eyes_event = next(e for e in report.events if e.file_name == "blue_eyes.jpg")
        assert blue_eyes_event.decision == "pending"
        assert blue_eyes_event.card_name == "Blue-Eyes White Dragon"  # a leitura não some


class TestFailureIsolation:
    """Requisito explícito do briefing §13: uma imagem ruim não trava as boas."""

    def test_broken_image_does_not_stop_the_others(
        self, catalog: Database, settings: Settings, cards_folder: Path
    ) -> None:
        make_corrupted_image(cards_folder / "quebrada.jpg")

        report = scan(catalog, settings, cards_folder)

        assert report.total_images == 5
        assert report.failed == 1
        assert report.auto_added == 3  # as boas continuam entrando

    def test_broken_image_gets_a_scan_image_row_without_result(
        self, catalog: Database, settings: Settings, cards_folder: Path
    ) -> None:
        make_corrupted_image(cards_folder / "quebrada.jpg")
        scan(catalog, settings, cards_folder)

        with catalog.session() as session:
            broken = session.execute(
                select(ScanImage).where(ScanImage.file_path.like("%quebrada.jpg"))
            ).scalar_one()
            assert broken.status == "invalid"
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(ScanResult)
                    .where(ScanResult.scan_image_id == broken.id)
                )
                == 0
            )


class TestEmptyFolder:
    def test_no_images_is_a_clean_no_op(
        self, catalog: Database, settings: Settings, tmp_path: Path
    ) -> None:
        empty = tmp_path / "vazia"
        empty.mkdir()
        report = scan(catalog, settings, empty)
        assert report.total_images == 0
        assert report.job_id is None


class TestReview:
    """`confirm_result`/`reject_result`/`pending_results` — a fila do `review`
    (Fase 6). Usa `--no-auto` para produzir pendências determinísticas."""

    def test_pending_results_lists_what_scan_left_open(
        self, catalog: Database, settings: Settings, cards_folder: Path
    ) -> None:
        scan(catalog, settings, cards_folder, no_auto=True)
        pending = service(catalog, settings).pending_results()
        assert len(pending) == 4
        # A relação `scan_image` precisa estar acessível fora da sessão que
        # o método usou internamente (plano: sem DetachedInstanceError).
        assert all(result.scan_image.file_path for result in pending)

    def test_confirm_applies_to_the_collection(
        self, catalog: Database, settings: Settings, cards_folder: Path
    ) -> None:
        scan(catalog, settings, cards_folder, no_auto=True)
        svc = service(catalog, settings)
        pending = svc.pending_results()
        blue_eyes_result = next(r for r in pending if r.ocr_name_raw == "Blue-Eyes White Dragon")

        item = svc.confirm_result(blue_eyes_result.id, card_id=BLUE_EYES, card_print_id=None)

        assert item.card_id == BLUE_EYES
        assert item.quantity == 1
        # Objeto devolvido não pode estar detached (plano: DetachedInstanceError).
        assert item.card.name == "Blue-Eyes White Dragon"

    def test_confirmed_result_leaves_the_pending_queue(
        self, catalog: Database, settings: Settings, cards_folder: Path
    ) -> None:
        scan(catalog, settings, cards_folder, no_auto=True)
        svc = service(catalog, settings)
        target = svc.pending_results()[0]

        svc.confirm_result(target.id, card_id=BLUE_EYES, card_print_id=None)

        remaining_ids = {r.id for r in svc.pending_results()}
        assert target.id not in remaining_ids

    def test_confirm_twice_raises(
        self, catalog: Database, settings: Settings, cards_folder: Path
    ) -> None:
        from yugioh_scanner.errors import ScanResultAlreadyAppliedError

        scan(catalog, settings, cards_folder, no_auto=True)
        svc = service(catalog, settings)
        target = svc.pending_results()[0]
        svc.confirm_result(target.id, card_id=BLUE_EYES, card_print_id=None)

        with pytest.raises(ScanResultAlreadyAppliedError):
            svc.confirm_result(target.id, card_id=BLUE_EYES, card_print_id=None)

    def test_confirming_unknown_result_raises(self, catalog: Database, settings: Settings) -> None:
        from yugioh_scanner.errors import ScanResultNotFoundError

        with pytest.raises(ScanResultNotFoundError):
            service(catalog, settings).confirm_result(999999, card_id=BLUE_EYES)

    def test_reject_leaves_the_collection_untouched(
        self, catalog: Database, settings: Settings, cards_folder: Path
    ) -> None:
        scan(catalog, settings, cards_folder, no_auto=True)
        svc = service(catalog, settings)
        target = svc.pending_results()[0]

        svc.reject_result(target.id)

        assert target.id not in {r.id for r in svc.pending_results()}
        assert snapshot(catalog) == []

    def test_rejecting_unknown_result_raises(self, catalog: Database, settings: Settings) -> None:
        from yugioh_scanner.errors import ScanResultNotFoundError

        with pytest.raises(ScanResultNotFoundError):
            service(catalog, settings).reject_result(999999)

    def test_confirming_a_different_card_than_the_reading_works(
        self, catalog: Database, settings: Settings, cards_folder: Path
    ) -> None:
        """A revisão humana pode corrigir a leitura, não só confirmá-la."""
        scan(catalog, settings, cards_folder, no_auto=True)
        svc = service(catalog, settings)
        target = svc.pending_results()[0]

        item = svc.confirm_result(target.id, card_id=DARK_MAGICIAN, card_print_id=None)
        assert item.card_id == DARK_MAGICIAN

    def test_confirm_falls_back_to_the_detected_language_when_omitted(
        self, catalog: Database, settings: Settings, tmp_path: Path
    ) -> None:
        """Continuação do plano de idiomas: sem `language` explícito na
        confirmação, o que o matching detectou sozinho prevalece."""
        folder = tmp_path / "cards_lang"
        make_card_image(folder / "mago_negro.jpg", name="Mago Negro")
        register_provider(
            "fake", lambda _settings: FakeOCRProvider({"mago_negro.jpg": {"name": "Mago Negro"}})
        )
        reset_provider()

        scan(catalog, settings, folder, no_auto=True)
        svc = service(catalog, settings)
        target = svc.pending_results()[0]
        assert target.detected_language == "PT"

        item = svc.confirm_result(target.id, card_id=DARK_MAGICIAN, card_print_id=None)
        assert item.language == "PT"

    def test_confirm_with_explicit_language_overrides_the_detected_one(
        self, catalog: Database, settings: Settings, cards_folder: Path
    ) -> None:
        scan(catalog, settings, cards_folder, no_auto=True)
        svc = service(catalog, settings)
        blue_eyes_result = next(
            r for r in svc.pending_results() if r.ocr_name_raw == "Blue-Eyes White Dragon"
        )
        assert blue_eyes_result.detected_language == "EN"

        item = svc.confirm_result(
            blue_eyes_result.id, card_id=BLUE_EYES, card_print_id=None, language="DE"
        )
        assert item.language == "DE"


class TestJobIntrospection:
    def test_recent_jobs_lists_newest_first(
        self, catalog: Database, settings: Settings, cards_folder: Path, tmp_path: Path
    ) -> None:
        first = scan(catalog, settings, cards_folder)
        second_folder = tmp_path / "outra"
        second_folder.mkdir()
        make_card_image(second_folder / "unica.jpg", name="Outra Carta")
        second = scan(catalog, settings, second_folder)

        jobs = service(catalog, settings).recent_jobs()
        assert [job.id for job in jobs[:2]] == [second.job_id, first.job_id]

    def test_job_detail_matches_the_report(
        self, catalog: Database, settings: Settings, cards_folder: Path
    ) -> None:
        report = scan(catalog, settings, cards_folder)
        job = service(catalog, settings).job_detail(report.job_id)
        assert job.total_images == report.total_images
        assert job.auto_added == report.auto_added
        assert job.pending == report.pending
        assert job.status == "done"

    def test_unknown_job_raises(self, catalog: Database, settings: Settings) -> None:
        from yugioh_scanner.errors import JobNotFoundError

        with pytest.raises(JobNotFoundError):
            service(catalog, settings).job_detail(999999)
