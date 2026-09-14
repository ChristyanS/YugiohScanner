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

from tests.factories import make_card_image, make_corrupted_image, make_grid_photo
from yugioh_scanner.config import Settings
from yugioh_scanner.db.session import Database
from yugioh_scanner.db.tables import CardPrint, CollectionItem, ScanImage, ScanJob, ScanResult
from yugioh_scanner.images.preprocess import BoundingBox
from yugioh_scanner.ocr.base import BaseOCRProvider, OCRRequest, OCRResult, TextLine
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
    # A suíte deste arquivo testa coleção/idempotência/grade/fallback, não a
    # política "AUTO exige SET" (config.py::auto_requires_print, testada à
    # parte em `TestAutoRequiresSetPolicy`) — sem isto, "pot_of_greed.jpg"
    # (fixture de propósito sem código, comentário acima) rebaixaria para
    # PENDING sob o novo default e desalinharia os números de todos os
    # outros testes com uma política que não é o que eles exercitam.
    kwargs.setdefault("require_set", False)
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
            folder, provider_name="fake", workers=1, require_set=False
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

    def test_job_results_page_shows_the_results_that_job_actually_produced(
        self, catalog: Database, settings: Settings, cards_folder: Path
    ) -> None:
        """Achado real do usuário: `GET /scan/{id}` (`job_results`) filtrava
        pelo job que descobriu a `ScanImage` pela primeira vez — um
        `--reprocess` de uma pasta já vista aparecia vazio (scan carta a
        carta) ou parcial (grade), preso na tela do job original."""
        first = scan(catalog, settings, cards_folder)
        second = scan(catalog, settings, cards_folder, reprocess=True)

        svc = service(catalog, settings)
        first_results = svc.job_results(first.job_id)
        second_results = svc.job_results(second.job_id)

        assert len(first_results) == 4
        assert len(second_results) == 4  # não vazio, não parcial
        assert {r.id for r in first_results}.isdisjoint({r.id for r in second_results})

    def test_reprocess_reapplies_if_the_collection_item_was_deleted_out_of_band(
        self, catalog: Database, settings: Settings, cards_folder: Path
    ) -> None:
        """Achado real do usuário (Scan #71): a coleção ficou vazia por fora
        deste caminho (reset de banco durante um teste), mas os
        `ScanResult.applied=True` de um scan anterior continuavam lá —
        `has_applied_result` recusava reaplicar para sempre, mesmo a carta
        não existindo mais na coleção. A guarda precisa confirmar que o
        `collection_item` referenciado ainda existe, não só o flag."""
        scan(catalog, settings, cards_folder)
        assert len(snapshot(catalog)) > 0

        with catalog.session() as session:
            session.execute(CollectionItem.__table__.delete())
            session.commit()
        assert snapshot(catalog) == []

        report = scan(catalog, settings, cards_folder, reprocess=True)

        assert report.already_applied == 0  # reaplicou de verdade, não é no-op
        assert len(snapshot(catalog)) > 0  # a coleção voltou a ter as cartas


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


class TestRequireSetPolicy:
    """`config.Settings.auto_requires_print` (default `True`) e o override
    `scan(require_set=...)` — pedido real do usuário: um AUTO sem set
    identificado (`pot_of_greed.jpg`, sem código na fixture) nunca deveria
    passar batido, porque some a referência da foto de origem."""

    def test_default_setting_requires_print_for_auto(self, settings: Settings) -> None:
        assert settings.auto_requires_print is True

    def test_auto_without_print_downgrades_to_pending_by_default(
        self, catalog: Database, settings: Settings, cards_folder: Path
    ) -> None:
        # Sem `require_set=False` (diferente do helper `scan()` deste
        # arquivo): exercita o default de verdade de `Settings`.
        report = service(catalog, settings).scan(
            cards_folder, provider_name="fake", workers=1
        )
        pot_event = next(e for e in report.events if e.file_name == "pot_of_greed.jpg")
        assert pot_event.decision == "pending"
        assert pot_event.card_name == "Pot of Greed"  # identificado, só não auto

        blue_eyes_event = next(e for e in report.events if e.file_name == "blue_eyes.jpg")
        assert blue_eyes_event.decision == "auto"  # tinha código: não é afetado

        assert report.auto_added == 2
        assert report.pending == 2  # pot_of_greed.jpg + garbage.jpg

    def test_explicit_require_set_false_overrides_the_setting_default(
        self, catalog: Database, settings: Settings, cards_folder: Path
    ) -> None:
        report = service(catalog, settings).scan(
            cards_folder, provider_name="fake", workers=1, require_set=False
        )
        pot_event = next(e for e in report.events if e.file_name == "pot_of_greed.jpg")
        assert pot_event.decision == "auto"
        assert report.auto_added == 3

    def test_explicit_require_set_true_overrides_a_false_setting_default(
        self, catalog: Database, settings: Settings, cards_folder: Path
    ) -> None:
        lenient = settings.model_copy(update={"auto_requires_print": False})
        report = service(catalog, lenient).scan(
            cards_folder, provider_name="fake", workers=1, require_set=True
        )
        pot_event = next(e for e in report.events if e.file_name == "pot_of_greed.jpg")
        assert pot_event.decision == "pending"


class TestRequireRarityPolicy:
    """`config.Settings.auto_requires_rarity` (default `False`) e o override
    `scan(require_rarity=...)` — pedido real do usuário: quando o set já é
    100% identificado mas há 2+ raridades catalogadas para ele (`LOB-001`:
    Ultra e Secret Rare na fixture, mesmo caso de `test_matching.py::
    TestAmbiguousRarity`), a carta foi "analisada com sucesso" mesmo sem
    saber a raridade — por padrão isso não deveria bloquear o AUTO, só ficar
    pendente a escolha da raridade (`CollectionItem.set_code_full` +
    raridade em branco, resolvível depois em `/collection`)."""

    def test_default_setting_does_not_require_rarity(self, settings: Settings) -> None:
        assert settings.auto_requires_rarity is False

    def test_ambiguous_rarity_still_autos_by_default(
        self, catalog: Database, settings: Settings, tmp_path: Path
    ) -> None:
        folder = tmp_path / "cards"
        make_card_image(
            folder / "blue_eyes_lob.jpg", name="Blue-Eyes White Dragon", set_code="LOB-001"
        )
        register_provider(
            "fake-lob",
            lambda _settings: FakeOCRProvider(
                {"blue_eyes_lob.jpg": {"name": "Blue-Eyes White Dragon", "code": "LOB-001"}}
            ),
        )
        try:
            report = service(catalog, settings).scan(folder, provider_name="fake-lob", workers=1)
        finally:
            unregister_provider("fake-lob")

        assert report.events[0].decision == "auto"
        assert report.auto_added == 1

        with catalog.session() as session:
            item = session.execute(
                select(CollectionItem).where(CollectionItem.card_id == BLUE_EYES)
            ).scalar_one()
            assert item.card_print_id is None
            assert item.set_code_full == "LOB-001"
            assert item.rarity is None

    def test_explicit_require_rarity_true_downgrades_ambiguous_rarity(
        self, catalog: Database, settings: Settings, tmp_path: Path
    ) -> None:
        folder = tmp_path / "cards"
        make_card_image(
            folder / "blue_eyes_lob.jpg", name="Blue-Eyes White Dragon", set_code="LOB-001"
        )
        register_provider(
            "fake-lob-2",
            lambda _settings: FakeOCRProvider(
                {"blue_eyes_lob.jpg": {"name": "Blue-Eyes White Dragon", "code": "LOB-001"}}
            ),
        )
        try:
            report = service(catalog, settings).scan(
                folder, provider_name="fake-lob-2", workers=1, require_rarity=True
            )
        finally:
            unregister_provider("fake-lob-2")

        assert report.events[0].decision == "pending"
        assert report.auto_added == 0

    def test_unambiguous_set_still_autos_even_with_require_rarity(
        self, catalog: Database, settings: Settings, cards_folder: Path
    ) -> None:
        """Sets com uma raridade só (`CT13-EN008`, na fixture) já resolvem
        `card_print_id` sozinhos — não passam pelo desempate de raridade, e
        `--require-rarity` não deveria bloquear o que já é inequívoco."""
        report = service(catalog, settings).scan(
            cards_folder, provider_name="fake", workers=1, require_rarity=True
        )
        blue_eyes_event = next(e for e in report.events if e.file_name == "blue_eyes.jpg")
        assert blue_eyes_event.decision == "auto"


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

    def test_confirm_with_explicit_print_and_language_records_an_override(
        self, catalog: Database, settings: Settings, cards_folder: Path
    ) -> None:
        """ADR 0012 (Opção D): confirmar manualmente carta+print+idioma ensina
        o `OverridePrintLanguageResolver` para a próxima leitura dessa carta
        nesse idioma — mesmo sem nenhuma fonte externa saber a resposta."""
        from yugioh_scanner.repositories.print_override import PrintOverrideRepository

        scan(catalog, settings, cards_folder, no_auto=True)
        svc = service(catalog, settings)
        target = svc.pending_results()[0]

        with catalog.session() as session:
            sdk_print = session.scalar(
                select(CardPrint).where(CardPrint.set_code_full == "SDK-001")
            )
            assert sdk_print is not None
            print_id = sdk_print.id

        svc.confirm_result(
            target.id, card_id=DARK_MAGICIAN, card_print_id=print_id, language="DE"
        )

        with catalog.session() as session:
            found = PrintOverrideRepository(session).get_print(DARK_MAGICIAN, "DE")
            assert found is not None
            assert found.id == print_id

    def test_confirm_in_english_does_not_record_an_override(
        self, catalog: Database, settings: Settings, cards_folder: Path
    ) -> None:
        """Inglês é o print padrão — não há nada a "aprender" aqui."""
        from yugioh_scanner.repositories.print_override import PrintOverrideRepository

        scan(catalog, settings, cards_folder, no_auto=True)
        svc = service(catalog, settings)
        blue_eyes_result = next(
            r for r in svc.pending_results() if r.ocr_name_raw == "Blue-Eyes White Dragon"
        )

        with catalog.session() as session:
            ct13_print = session.scalar(
                select(CardPrint).where(CardPrint.set_code_full == "CT13-EN008")
            )
            assert ct13_print is not None
            print_id = ct13_print.id

        svc.confirm_result(
            blue_eyes_result.id, card_id=BLUE_EYES, card_print_id=print_id, language="EN"
        )

        with catalog.session() as session:
            assert PrintOverrideRepository(session).get_print(BLUE_EYES, "EN") is None


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


class _FallbackOCRProvider:
    """Fake do provider LLM (Fase 10) — mesma ideia do `FakeOCRProvider`, só
    que registrado como `claude` para exercitar a cascata (plano §6.3) sem
    tocar o SDK da Anthropic."""

    name = "claude"
    is_io_bound = True

    def __init__(self, script: dict[str, dict[str, str]]) -> None:
        self.script = script
        self.calls: list[str] = []

    def warmup(self) -> None:
        return

    def close(self) -> None:
        return

    def read(self, request: object) -> object:
        from yugioh_scanner.ocr.base import REGION_CODE, REGION_NAME, OCRResult, TextLine

        source = request.source  # type: ignore[attr-defined]
        self.calls.append(source)
        reading = self.script.get(Path(source).name, {})
        texts: dict[str, tuple[TextLine, ...]] = {}
        if reading.get("name"):
            texts[REGION_NAME] = (TextLine(text=reading["name"], confidence=1.0),)
        if reading.get("code"):
            texts[REGION_CODE] = (TextLine(text=reading["code"], confidence=1.0),)
        return OCRResult(texts=texts, provider=self.name)


class TestLlmFallback:
    """Cascata de fallback (plano §6.3, Fase 10): só a fração duvidosa
    reprocessa por LLM, e o resultado só é adotado quando melhora."""

    def test_fallback_rescues_an_unmatched_reading(
        self, catalog: Database, settings: Settings, cards_folder: Path
    ) -> None:
        fallback = _FallbackOCRProvider(
            {"garbage.jpg": {"name": "Blue-Eyes White Dragon", "code": "CT13-EN008"}}
        )
        register_provider("claude", lambda _settings: fallback)
        try:
            fb_settings = settings.model_copy(update={"ocr_fallback_provider": "claude"})
            report = scan(catalog, fb_settings, cards_folder)
        finally:
            unregister_provider("claude")

        assert report.auto_added == 4  # os 3 de sempre + garbage.jpg resgatado
        assert report.pending == 0
        # Só a leitura duvidosa (sem candidato) disparou a cascata — as três
        # que já resolveram por OCR local não pagam o custo extra.
        assert [Path(call).name for call in fallback.calls] == ["garbage.jpg"]

        # Regressão: quando o fallback é adotado, o texto bruto persistido
        # precisa ser o que ele leu, não o "ZZQX WROMBAT FLURB" do worker
        # original — senão a revisão mostraria "(vazio)"/lixo ao lado de uma
        # carta corretamente identificada.
        with catalog.session() as session:
            image = session.execute(
                select(ScanImage).where(ScanImage.file_path.like("%garbage.jpg"))
            ).scalar_one()
            result = session.execute(
                select(ScanResult).where(ScanResult.scan_image_id == image.id)
            ).scalar_one()
            assert result.ocr_name_raw == "Blue-Eyes White Dragon"
            assert result.ocr_code_raw == "CT13-EN008"

    def test_fallback_disabled_by_default(
        self, catalog: Database, settings: Settings, cards_folder: Path
    ) -> None:
        fallback = _FallbackOCRProvider(
            {"garbage.jpg": {"name": "Blue-Eyes White Dragon", "code": "CT13-EN008"}}
        )
        register_provider("claude", lambda _settings: fallback)
        try:
            report = scan(catalog, settings, cards_folder)  # ocr_fallback_provider="none"
        finally:
            unregister_provider("claude")

        assert report.pending == 1  # garbage.jpg continua sem candidato
        assert fallback.calls == []

    def test_fallback_without_credentials_degrades_silently(
        self, catalog: Database, settings: Settings, cards_folder: Path
    ) -> None:
        """Sem `register_provider`, isto usa o `ClaudeVisionOCRProvider` de
        verdade — sem `ANTHROPIC_API_KEY` no ambiente (garantido pelo
        `_isolate_settings` de `conftest.py`), a cascata falha ao aquecer e
        degrada em silêncio (ADR 0004), sem derrubar o scan."""
        fb_settings = settings.model_copy(update={"ocr_fallback_provider": "claude"})

        report = scan(catalog, fb_settings, cards_folder)

        assert report.failed == 0
        assert report.pending == 1  # garbage.jpg continua sem candidato


class TestReviewCodePrints:
    """`ScanService._attach_code_prints`: a revisão precisa do código lido
    resolvido contra o catálogo **de novo**, não só do print único que o
    motor gravou em `card_print_id` — é o que permite preencher o set certo
    mesmo trocando de candidato, e oferecer as raridades certas quando o
    código sozinho não desempata (achado real do usuário: set vazio/errado
    ao escolher outro candidato na revisão)."""

    def test_unambiguous_code_resolves_to_a_single_print(
        self, catalog: Database, settings: Settings, cards_folder: Path
    ) -> None:
        scan(catalog, settings, cards_folder, no_auto=True)
        svc = service(catalog, settings)
        dark_magician_result = next(
            r for r in svc.pending_results() if r.ocr_name_raw == "Dark Magician"
        )

        assert [p["card_id"] for p in dark_magician_result.code_prints] == [DARK_MAGICIAN]
        assert dark_magician_result.code_prints[0]["set_code"] == "SDK-001"

    def test_ambiguous_rarity_exposes_every_print_for_the_user_to_pick(
        self, catalog: Database, settings: Settings, tmp_path: Path
    ) -> None:
        """`LOB-001` existe em duas raridades da mesma carta na fixture
        (plano §0.3.3) — o motor não pode adivinhar qual, mas a revisão
        precisa mostrar as duas, não nenhuma."""
        folder = tmp_path / "lob"
        make_card_image(folder / "blue_eyes_lob.jpg", name="Blue-Eyes White Dragon", set_code="LOB-001")
        register_provider(
            "fake-lob",
            lambda _settings: FakeOCRProvider(
                {"blue_eyes_lob.jpg": {"name": "Blue-Eyes White Dragon", "code": "LOB-001"}}
            ),
        )
        try:
            scan(catalog, settings, folder, provider_name="fake-lob", no_auto=True)
        finally:
            unregister_provider("fake-lob")

        svc = service(catalog, settings)
        result = next(r for r in svc.pending_results() if r.ocr_code_raw == "LOB-001")

        assert len(result.code_prints) == 2
        assert {p["card_id"] for p in result.code_prints} == {BLUE_EYES}
        assert {p["rarity"] for p in result.code_prints} == {"Ultra Rare", "Secret Rare"}

    def test_get_result_also_attaches_code_prints(
        self, catalog: Database, settings: Settings, cards_folder: Path
    ) -> None:
        scan(catalog, settings, cards_folder, no_auto=True)
        svc = service(catalog, settings)
        dark_magician_result = next(
            r for r in svc.pending_results() if r.ocr_name_raw == "Dark Magician"
        )

        detail = svc.get_result(dark_magician_result.id)

        assert [p["card_id"] for p in detail.code_prints] == [DARK_MAGICIAN]


class _SequentialOCRProvider(BaseOCRProvider):
    """Cada `read()` devolve a próxima leitura da lista, na ordem.

    `FakeOCRProvider` roteiriza só por nome de arquivo (plano §19.3) — não
    serve para uma foto-grade, onde todos os recortes compartilham o mesmo
    arquivo de origem e precisam de leituras diferentes por chamada.
    """

    name = "fake-sequential"
    is_io_bound = False

    def __init__(self, readings: list[dict[str, str]]) -> None:
        self._readings = readings
        self._calls = 0

    def read(self, request: OCRRequest) -> OCRResult:
        texts = self._readings[self._calls % len(self._readings)]
        self._calls += 1
        return OCRResult(
            texts={
                region: (TextLine(text=value, confidence=0.95),)
                for region, value in texts.items()
                if value
            },
            provider=self.name,
            elapsed_ms=1,
        )


#: Mesmas leituras de `SCRIPT`, na ordem em que os recortes aparecem na foto
#: composta: blue-eyes e dark-magician casam (com código), pot-of-greed casa
#: sem código (print NULL), garbage não casa com nada.
GRID_READINGS = [
    {"name": "Blue-Eyes White Dragon", "code": "CT13-EN008"},
    {"name": "Dark Magician", "code": "SDK-001"},
    {"name": "Pot of Greed"},
    {"name": "ZZQX WROMBAT FLURB"},
]


class TestGridMode:
    """Grade de cartas por foto (plano §22, ADR 0011) — flag `--grid`."""

    @pytest.fixture(autouse=True)
    def _fake_sequential_provider(self) -> Iterator[None]:
        register_provider(
            "fake-sequential", lambda _settings: _SequentialOCRProvider(GRID_READINGS)
        )
        try:
            yield
        finally:
            unregister_provider("fake-sequential")
            reset_provider()

    def _patch_grid_detection(
        self, monkeypatch: pytest.MonkeyPatch, boxes: list[BoundingBox]
    ) -> None:
        # `ensure_cv_available` real faria `import cv2` de verdade — a suíte
        # rápida não pode exigir o extra `cv` instalado. `detect_grid_cells`
        # fakeado devolve caixas conhecidas em vez de detectar de verdade,
        # deixando o teste focado no fan-out do pipeline, não na qualidade
        # da detecção (isso é assunto de `tests/unit/test_grid.py`).
        monkeypatch.setattr(
            "yugioh_scanner.services.scan_service.ensure_cv_available", lambda: None
        )
        monkeypatch.setattr(
            "yugioh_scanner.scanner.worker.detect_grid_cells", lambda _image: list(boxes)
        )

    def test_grid_scan_produces_one_result_per_card(
        self,
        catalog: Database,
        settings: Settings,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        folder = tmp_path / "grid"
        _photo, boxes = make_grid_photo(folder, "sheet.jpg", GRID_READINGS)
        self._patch_grid_detection(monkeypatch, boxes)

        report = scan(
            catalog, settings, folder, provider_name="fake-sequential", workers=1, grid=True
        )

        assert report.total_images == 1
        assert len(report.events) == 4
        assert {event.crop_count for event in report.events} == {4}
        assert sorted(event.crop_index for event in report.events) == [0, 1, 2, 3]
        assert report.auto_added == 3  # tudo, menos o garbage
        assert report.pending == 1
        assert report.failed == 0

        with catalog.session() as session:
            image = session.execute(
                select(ScanImage).where(ScanImage.file_path.like("%sheet.jpg"))
            ).scalar_one()
            results = (
                session.execute(select(ScanResult).where(ScanResult.scan_image_id == image.id))
                .scalars()
                .all()
            )
            assert len(results) == 4
            assert sorted(r.crop_index for r in results) == [0, 1, 2, 3]
            assert all(r.crop_count == 4 for r in results)
            assert all(r.source_bbox_left is not None for r in results)

    def test_grid_disabled_by_default_treats_photo_as_single_card(
        self,
        catalog: Database,
        settings: Settings,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        folder = tmp_path / "grid"
        make_grid_photo(folder, "sheet.jpg", GRID_READINGS)
        # `detect_grid_cells` não é fakeado de propósito: sem `--grid`, o
        # worker nem deveria chamá-lo (plano §22 — o caminho de hoje não pode
        # nem importar `images/grid.py`, muito menos o `cv2` por trás dele).

        report = scan(catalog, settings, folder, provider_name="fake-sequential", workers=1)

        assert report.total_images == 1
        assert len(report.events) == 1
        assert report.events[0].crop_count == 1
        assert report.events[0].crop_index == 0

    def test_single_card_photo_with_grid_enabled_is_unaffected(
        self,
        catalog: Database,
        settings: Settings,
        cards_folder: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Regressão: `--grid` não pode mudar o resultado de fotos normais
        quando nenhuma grade é detectada — o caso comum (fotos avulsas)."""
        monkeypatch.setattr(
            "yugioh_scanner.services.scan_service.ensure_cv_available", lambda: None
        )
        monkeypatch.setattr("yugioh_scanner.scanner.worker.detect_grid_cells", lambda _image: [])

        report = scan(catalog, settings, cards_folder, grid=True)

        # Mesmos números do baseline sem --grid (test_high_confidence_matches_are_applied).
        assert report.total_images == 4
        assert report.auto_added == 3
        assert report.pending == 1
        assert report.failed == 0
        assert all(event.crop_count == 1 for event in report.events)

    def test_reprocess_grid_photo_does_not_reapply_already_applied_crop(
        self,
        catalog: Database,
        settings: Settings,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Exercita a correção de `has_applied_result` (plano §22): sem o
        filtro por `crop_index`, o recorte #0 aplicado bloquearia os demais
        recortes da mesma `ScanImage` — tanto no scan original quanto num
        `--reprocess` seguinte."""
        folder = tmp_path / "grid"
        _photo, boxes = make_grid_photo(folder, "sheet.jpg", GRID_READINGS)
        self._patch_grid_detection(monkeypatch, boxes)

        first = scan(
            catalog, settings, folder, provider_name="fake-sequential", workers=1, grid=True
        )
        assert first.auto_added == 3

        second = scan(
            catalog,
            settings,
            folder,
            provider_name="fake-sequential",
            workers=1,
            grid=True,
            reprocess=True,
        )

        # Nada duplica: as 3 cartas continuam contando 1x cada na coleção.
        items = snapshot(catalog)
        assert len(items) == 3
        assert all(quantity == 1 for _, _, quantity in items)
        # A decisão do motor continua `auto` para as 3 (mesmo valor mostrado
        # no log por imagem) — só não escreveram nada novo, por já terem
        # sido aplicados no scan anterior.
        assert second.auto_added == 3
        assert second.already_applied == 3

    def test_grid_size_explicit_needs_no_cv2_mocking(
        self, catalog: Database, settings: Settings, tmp_path: Path
    ) -> None:
        """`grid_size` (`--grid-size`) pula `detect_grid_cells`/
        `ensure_cv_available` de vez — ao contrário dos testes acima, este
        não precisa fakear nada de `cv2` (plano §22: o achado real foi que a
        detecção automática por contorno erra em fotos de verdade; o modo
        manual existe justamente para não depender dela)."""
        folder = tmp_path / "grid"
        make_grid_photo(folder, "sheet.jpg", GRID_READINGS)

        report = scan(
            catalog,
            settings,
            folder,
            provider_name="fake-sequential",
            workers=1,
            grid_size=(2, 2),
        )

        assert report.total_images == 1
        assert len(report.events) == 4
        assert sorted(event.crop_index for event in report.events) == [0, 1, 2, 3]
        assert all(event.crop_count == 4 for event in report.events)
        assert report.auto_added == 3  # tudo, menos o garbage
        assert report.pending == 1
        assert report.failed == 0

    def test_grid_size_skips_blank_cells_beyond_the_real_cards(
        self, catalog: Database, settings: Settings, tmp_path: Path
    ) -> None:
        """Achado real do usuário: uma folha de fichário pode ter menos
        cartas que células no layout informado (ex.: 7 de 9 numa grade 3x3).
        `make_grid_photo` com 3 leituras compõe um canvas 2x2 (arredondado
        para cima) — a 4ª célula nunca recebe carta nenhuma, ficando com a
        cor de fundo sólida do canvas: exatamente uma célula vazia de verdade."""
        folder = tmp_path / "grid"
        make_grid_photo(folder, "sheet.jpg", GRID_READINGS[:3])

        report = scan(
            catalog,
            settings,
            folder,
            provider_name="fake-sequential",
            workers=1,
            grid_size=(2, 2),
        )

        assert report.total_images == 1
        assert report.skipped_empty == 1
        assert len(report.events) == 3  # não 4 — a célula vazia nunca virou ScanResult
        assert sorted(event.crop_index for event in report.events) == [0, 1, 2]
        assert all(event.crop_count == 3 for event in report.events)
        assert report.auto_added == 3  # as 3 cartas reais — nenhuma é o garbage
        assert report.pending == 0
        assert report.failed == 0


class TestPasscodeIdentity:
    """Passcode ("Card ID", canto inferior-esquerdo) como fonte primária de
    identidade — ponta a ponta: nome ilegível, passcode sozinho identifica a
    carta (pedido real do usuário; unidade em `tests/integration/test_matching.py`)."""

    @pytest.fixture(autouse=True)
    def _fake_passcode_provider(self) -> Iterator[None]:
        register_provider(
            "fake-passcode",
            lambda _settings: FakeOCRProvider(
                {"garbled.jpg": {"name": "ZZQX WROMBAT FLURB", "passcode": str(BLUE_EYES)}}
            ),
        )
        try:
            yield
        finally:
            unregister_provider("fake-passcode")
            reset_provider()

    def test_passcode_alone_identifies_the_card_end_to_end(
        self, catalog: Database, settings: Settings, tmp_path: Path
    ) -> None:
        folder = tmp_path / "passcode"
        make_card_image(folder / "garbled.jpg", name="ZZQX WROMBAT FLURB", set_code="LOB-001")

        report = scan(catalog, settings, folder, provider_name="fake-passcode", workers=1)

        assert report.auto_added == 1
        assert report.pending == 0
        event = report.events[0]
        assert event.card_name == "Blue-Eyes White Dragon"
        assert event.passcode_verified is True
        assert event.decision == "auto"
        assert event.ocr_passcode == str(BLUE_EYES)

        items = snapshot(catalog)
        assert (BLUE_EYES, None, 1) in items  # sem código lido: identidade sim, print não
