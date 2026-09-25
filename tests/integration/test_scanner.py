"""Pipeline do scanner até o OCR (Fase 3).

Usa o `FakeOCRProvider`: determinístico, sem modelo para carregar e sem rede.
Cobre descoberta, pré-processamento, paralelismo e isolamento de falhas — o
grosso da lógica do scanner — sem depender de OCR real.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.factories import make_card_image, make_corrupted_image, make_truncated_jpeg
from yugioh_scanner.config import MAX_WORKERS, Settings
from yugioh_scanner.ocr.base import OCRRequest, OCRResult, TextLine
from yugioh_scanner.ocr.fake_provider import FakeOCRProvider
from yugioh_scanner.ocr.registry import (
    create_provider,
    known_providers,
    register_provider,
    unregister_provider,
)
from yugioh_scanner.scanner.discovery import discover_images
from yugioh_scanner.scanner.executor import (
    SerialScanExecutor,
    ThreadPoolScanExecutor,
    create_executor,
    resolve_workers,
)
from yugioh_scanner.scanner.worker import ScanTask, process_task, reset_provider, set_provider


@pytest.fixture(autouse=True)
def _clean_worker_state() -> Iterator[None]:
    """O provider do worker é estado global do processo — limpar entre testes."""
    yield
    reset_provider()


@pytest.fixture
def cards_folder(tmp_path: Path) -> Path:
    folder = tmp_path / "cards"
    make_card_image(folder / "IMG_001.jpg", name="BLUE-EYES WHITE DRAGON", set_code="LOB-001")
    make_card_image(folder / "IMG_002.jpg", name="DARK MAGICIAN", set_code="SDY-006")
    make_card_image(folder / "IMG_003.jpg", name="POT OF GREED", set_code="LOB-119")
    return folder


def tasks_for(folder: Path) -> list[ScanTask]:
    return [
        ScanTask(path=image.path, file_hash=image.file_hash, size=image.size)
        for image in discover_images(folder)
    ]


class TestWorker:
    def test_processes_a_valid_image(self, cards_folder: Path, settings: Settings) -> None:
        provider = FakeOCRProvider(
            {"IMG_001.jpg": {"name": "BLUE-EYES WHITE DRAGON", "code": "LOB-001"}}
        )
        set_provider(provider, settings)

        outcome = process_task(tasks_for(cards_folder)[0])
        assert outcome.preprocess_status is None
        assert len(outcome.crops) == 1
        crop = outcome.crops[0]
        assert crop.region.index == 0
        assert crop.region.count == 1
        assert crop.status == "ok"
        assert crop.ocr is not None
        assert crop.ocr.best("name") == "BLUE-EYES WHITE DRAGON"
        assert crop.ocr.best("code") == "LOB-001"

    def test_invalid_image_becomes_data_not_exception(
        self, tmp_path: Path, settings: Settings
    ) -> None:
        """Erro por imagem é dado (plano §16) — o worker nunca levanta."""
        set_provider(FakeOCRProvider(), settings)
        path = make_corrupted_image(tmp_path / "lixo.jpg")

        outcome = process_task(ScanTask(path=path, file_hash="x"))
        assert outcome.preprocess_status == "invalid"
        assert outcome.preprocess_error is not None
        assert "lixo.jpg" in outcome.preprocess_error, "o erro precisa dizer QUAL imagem falhou"
        assert outcome.crops == []

    def test_truncated_image(self, tmp_path: Path, settings: Settings) -> None:
        set_provider(FakeOCRProvider(), settings)
        outcome = process_task(
            ScanTask(path=make_truncated_jpeg(tmp_path / "meio.jpg"), file_hash="x")
        )
        assert outcome.preprocess_status == "invalid"

    def test_ocr_exception_is_captured(self, cards_folder: Path, settings: Settings) -> None:
        set_provider(FakeOCRProvider(fail_on={"IMG_001.jpg"}), settings)
        outcome = process_task(tasks_for(cards_folder)[0])
        crop = outcome.crops[0]
        assert crop.status == "error"
        assert "Falha simulada" in (crop.error or "")

    def test_empty_ocr_is_its_own_status(self, cards_folder: Path, settings: Settings) -> None:
        """Sem texto ≠ erro: a imagem foi lida, só não rendeu nada."""
        set_provider(FakeOCRProvider(default={}), settings)
        outcome = process_task(tasks_for(cards_folder)[0])
        crop = outcome.crops[0]
        assert crop.status == "ocr_empty"
        assert crop.error is None

    def test_records_timing(self, cards_folder: Path, settings: Settings) -> None:
        set_provider(FakeOCRProvider(default={"name": "X"}), settings)
        assert process_task(tasks_for(cards_folder)[0]).elapsed_ms >= 0


class TestExecutors:
    def test_serial_processes_everything(self, cards_folder: Path, settings: Settings) -> None:
        provider = FakeOCRProvider(default={"name": "X"})
        executor = SerialScanExecutor(provider, settings)
        assert len(list(executor.map(tasks_for(cards_folder)))) == 3

    def test_serial_warms_up_once(self, cards_folder: Path, settings: Settings) -> None:
        """O modelo carrega por worker, nunca por imagem (plano §12.2)."""
        provider = FakeOCRProvider(default={"name": "X"})
        list(SerialScanExecutor(provider, settings).map(tasks_for(cards_folder)))
        assert provider.warmup_calls == 1
        assert provider.read_calls == 3

    def test_thread_pool_processes_everything(self, cards_folder: Path, settings: Settings) -> None:
        provider = FakeOCRProvider(default={"name": "X"})
        executor = ThreadPoolScanExecutor(provider, settings, workers=3)
        assert len(list(executor.map(tasks_for(cards_folder)))) == 3

    def test_thread_pool_warms_up_once(self, cards_folder: Path, settings: Settings) -> None:
        provider = FakeOCRProvider(default={"name": "X"})
        list(ThreadPoolScanExecutor(provider, settings, workers=3).map(tasks_for(cards_folder)))
        assert provider.warmup_calls == 1

    def test_one_failure_does_not_stop_the_others(
        self, cards_folder: Path, settings: Settings
    ) -> None:
        """O requisito explícito do briefing §13."""
        provider = FakeOCRProvider(default={"name": "X"}, fail_on={"IMG_002.jpg"})
        outcomes = list(ThreadPoolScanExecutor(provider, settings, 3).map(tasks_for(cards_folder)))

        by_name = {outcome.path.name: outcome for outcome in outcomes}
        assert len(outcomes) == 3
        assert by_name["IMG_002.jpg"].crops[0].status == "error"
        assert by_name["IMG_001.jpg"].crops[0].status == "ok"
        assert by_name["IMG_003.jpg"].crops[0].status == "ok"

    def test_empty_task_list(self, settings: Settings) -> None:
        provider = FakeOCRProvider()
        assert list(ThreadPoolScanExecutor(provider, settings, 2).map([])) == []

    def test_stop_check_cancels(self, cards_folder: Path, settings: Settings) -> None:
        """O cancelamento da UI web precisa funcionar no meio do job."""
        provider = FakeOCRProvider(default={"name": "X"})
        stopped = {"value": False}

        executor = ThreadPoolScanExecutor(
            provider, settings, workers=1, should_stop=lambda: stopped["value"]
        )
        produced = []
        for outcome in executor.map(tasks_for(cards_folder)):
            produced.append(outcome)
            stopped["value"] = True
        assert len(produced) < 3


class TestHeartbeat:
    """Uma foto de grade processa todas as células numa `Future` só (o
    fan-out acontece dentro do worker, `worker.py::process_task`) — sem
    resultado parcial, o pool fica mudo até a foto inteira terminar. O
    `on_heartbeat` existe para a Web avisar "ainda processando" nesse
    silêncio (achado real do usuário: log parecia travado numa grade
    lenta)."""

    def test_fires_while_a_slow_task_is_still_in_flight(
        self, cards_folder: Path, settings: Settings
    ) -> None:
        class SlowProvider(FakeOCRProvider):
            def read(self, request: OCRRequest) -> OCRResult:
                time.sleep(0.15)
                return super().read(request)

        provider = SlowProvider(default={"name": "X"})
        executor = ThreadPoolScanExecutor(provider, settings, workers=1)
        heartbeats: list[list[str]] = []

        outcomes = list(
            executor.map(
                tasks_for(cards_folder),
                on_heartbeat=lambda tasks: heartbeats.append([t.path.name for t in tasks]),
                heartbeat_interval=0.05,
            )
        )

        assert len(outcomes) == 3
        assert heartbeats, "esperava ao menos um pulso enquanto a 1ª tarefa lenta ainda rodava"
        # `workers=1` mas a janela de submissão é `workers * 2` (2 tarefas
        # enfileiradas mesmo com só 1 thread ativa) — o pulso reporta tudo
        # que já foi submetido e ainda não terminou, não só o que está de
        # fato rodando agora.
        assert "IMG_001.jpg" in heartbeats[0]

    def test_never_fires_once_everything_is_already_done(
        self, cards_folder: Path, settings: Settings
    ) -> None:
        """Provider rápido (sem `sleep`): nada fica pendente tempo o
        bastante pro timeout do pulso bater."""
        provider = FakeOCRProvider(default={"name": "X"})
        executor = ThreadPoolScanExecutor(provider, settings, workers=3)
        heartbeats: list[list[str]] = []

        list(
            executor.map(
                tasks_for(cards_folder),
                on_heartbeat=lambda tasks: heartbeats.append([t.path.name for t in tasks]),
                heartbeat_interval=5.0,
            )
        )

        assert heartbeats == []

    def test_without_on_heartbeat_behaves_exactly_like_before(
        self, cards_folder: Path, settings: Settings
    ) -> None:
        """Sem `on_heartbeat`, o `wait()` continua bloqueando sem prazo —
        mesmo comportamento de antes desta feature existir."""
        provider = FakeOCRProvider(default={"name": "X"})
        executor = ThreadPoolScanExecutor(provider, settings, workers=3)
        assert len(list(executor.map(tasks_for(cards_folder)))) == 3


class TestExecutorSelection:
    def test_single_worker_is_serial(self, settings: Settings) -> None:
        executor = create_executor(settings, provider_name="fake", workers=1)
        assert isinstance(executor, SerialScanExecutor)

    def test_io_bound_provider_uses_threads(self, settings: Settings) -> None:
        """OCR por LLM espera rede: processos seriam desperdício (§12.1)."""

        class RemoteProvider(FakeOCRProvider):
            name = "remoto"
            is_io_bound = True

        register_provider("remoto", lambda _settings: RemoteProvider())
        try:
            executor = create_executor(settings, provider_name="remoto", workers=4)
            assert isinstance(executor, ThreadPoolScanExecutor)
        finally:
            unregister_provider("remoto")

    def test_cpu_bound_provider_uses_processes(self, settings: Settings) -> None:
        from yugioh_scanner.scanner.executor import ProcessPoolScanExecutor

        executor = create_executor(settings, provider_name="fake", workers=4)
        assert isinstance(executor, ProcessPoolScanExecutor)
        executor.shutdown()

    def test_injected_provider_avoids_process_pool(self, settings: Settings) -> None:
        """Um provider injetado não sobrevive ao `spawn` do Windows."""
        executor = create_executor(settings, workers=4, provider=FakeOCRProvider())
        assert isinstance(executor, ThreadPoolScanExecutor)


class TestWorkerCount:
    def test_explicit_value_is_respected(self, settings: Settings) -> None:
        assert resolve_workers(settings, 3) == 3

    def test_capped(self, settings: Settings) -> None:
        assert resolve_workers(settings, 999) == MAX_WORKERS

    def test_zero_becomes_one(self, settings: Settings) -> None:
        assert resolve_workers(settings, 0) == 1

    def test_auto_uses_settings(self, settings: Settings) -> None:
        assert resolve_workers(settings, None) == settings.effective_workers


class TestRegistry:
    def test_known_providers_include_the_defaults(self) -> None:
        assert {"rapidocr", "tesseract", "fake"} <= set(known_providers())

    def test_unknown_provider_gives_an_actionable_error(self, settings: Settings) -> None:
        from yugioh_scanner.errors import OcrProviderError

        with pytest.raises(OcrProviderError, match="desconhecido"):
            create_provider("telepatia", settings)

    def test_override_wins(self, settings: Settings) -> None:
        sentinel = FakeOCRProvider(default={"name": "injetado"})
        register_provider("rapidocr", lambda _settings: sentinel)
        try:
            assert create_provider("rapidocr", settings) is sentinel
        finally:
            unregister_provider("rapidocr")


class TestOCRResult:
    def test_best_picks_highest_confidence(self) -> None:
        result = OCRResult(
            texts={"name": (TextLine("ERRADO", 0.4), TextLine("CERTO", 0.9))},
            provider="fake",
        )
        assert result.best("name") == "CERTO"

    def test_missing_region_is_empty_string(self) -> None:
        assert OCRResult(texts={}, provider="fake").best("name") == ""

    def test_joined_preserves_order(self) -> None:
        result = OCRResult(
            texts={"full": (TextLine("BLUE-EYES"), TextLine("LOB-001"))}, provider="fake"
        )
        assert result.joined("full") == "BLUE-EYES LOB-001"

    def test_is_empty(self) -> None:
        assert OCRResult(texts={"name": (TextLine("  "),)}, provider="fake").is_empty
        assert not OCRResult(texts={"name": (TextLine("X"),)}, provider="fake").is_empty

    def test_as_dict_is_serializable(self) -> None:
        import json

        result = OCRResult(
            texts={"name": (TextLine("Dark Magician", 0.98),)},
            provider="fake",
            elapsed_ms=42,
        )
        payload = json.loads(json.dumps(result.as_dict()))
        assert payload["provider"] == "fake"
        assert payload["texts"]["name"][0]["text"] == "Dark Magician"

    def test_confidence_average(self) -> None:
        result = OCRResult(
            texts={"name": (TextLine("a", 1.0), TextLine("b", 0.5))}, provider="fake"
        )
        assert result.confidence("name") == pytest.approx(0.75)


class TestFakeProvider:
    def test_script_by_filename(self, settings: Settings) -> None:
        provider = FakeOCRProvider({"a.jpg": {"name": "Dark Magician"}})
        result = provider.read(OCRRequest(regions={}, source="/qualquer/pasta/a.jpg"))
        assert result.best("name") == "Dark Magician"

    def test_falls_back_to_default(self) -> None:
        provider = FakeOCRProvider(default={"name": "Padrão"})
        assert provider.read(OCRRequest(regions={}, source="x.jpg")).best("name") == "Padrão"
