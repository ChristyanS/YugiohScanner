"""Comando `scan-device` pela CLI real (plano §22, ADR 0011).

O backend WIA é sempre mockado (`create_backend`) — sem hardware nem
`pywin32` de verdade instalado, mesmo padrão de `tests/unit/test_wia_capture.py`
e `tests/integration/test_web_api.py::TestCaptureApi`.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from yugioh_scanner.cli.main import app
from yugioh_scanner.config import reset_settings_cache
from yugioh_scanner.ocr.fake_provider import FakeOCRProvider
from yugioh_scanner.ocr.registry import register_provider, unregister_provider
from yugioh_scanner.scanner.worker import reset_provider

runner = CliRunner()


@pytest.fixture(autouse=True)
def scripted_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, catalog_template: Path
) -> Iterator[None]:
    data_path = tmp_path / "data"
    monkeypatch.setenv("YGS_DATA_PATH", str(data_path))
    monkeypatch.setenv("YGS_LOG_LEVEL", "WARNING")
    monkeypatch.setenv("YGS_OCR_PROVIDER", "roteiro")
    reset_settings_cache()

    data_path.mkdir(parents=True, exist_ok=True)
    shutil.copy2(catalog_template, data_path / "yugioh.db")

    register_provider("roteiro", lambda _settings: FakeOCRProvider({}))
    try:
        yield
    finally:
        unregister_provider("roteiro")
        reset_provider()
        reset_settings_cache()


class _FakeBackend:
    def __init__(self, device_infos: list[SimpleNamespace] | None = None) -> None:
        self.device_infos = device_infos or [SimpleNamespace(id="dev-1", name="Scanner A")]
        self.capture_calls: list[dict[str, Any]] = []

    def list_devices(self) -> list[SimpleNamespace]:
        return self.device_infos

    def capture(
        self, output_dir: Path, *, device_id: str | None = None, dpi: int = 300, color_mode: str = "color"
    ) -> Path:
        output_dir.mkdir(parents=True, exist_ok=True)
        path = output_dir / f"capture-{len(self.capture_calls) + 1}.jpg"
        from tests.factories import make_card_image

        make_card_image(path)
        self.capture_calls.append(
            {"output_dir": output_dir, "device_id": device_id, "dpi": dpi, "color_mode": color_mode}
        )
        return path


def _install_fake_backend(monkeypatch: pytest.MonkeyPatch, backend: _FakeBackend) -> None:
    monkeypatch.setattr(
        "yugioh_scanner.cli.capture_cmd.create_backend", lambda _name: backend
    )


class TestListDevices:
    def test_prints_devices(self, monkeypatch: pytest.MonkeyPatch) -> None:
        backend = _FakeBackend([SimpleNamespace(id="dev-1", name="Scanner A")])
        _install_fake_backend(monkeypatch, backend)

        result = runner.invoke(app, ["scan-device", "--list-devices"])

        assert result.exit_code == 0
        assert "Scanner A" in result.output


class TestSinglePage:
    def test_captures_once_without_prompting(self, monkeypatch: pytest.MonkeyPatch) -> None:
        backend = _FakeBackend()
        _install_fake_backend(monkeypatch, backend)

        result = runner.invoke(app, ["scan-device", "--workers", "1", "--dry-run", "--json"])

        assert result.exit_code == 0, result.output
        assert len(backend.capture_calls) == 1
        # Sem `--pages` > 1, não deve pedir confirmação nenhuma.
        assert "Pressione Enter" not in result.output


class TestMultiplePages:
    def test_pauses_between_captures(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """O achado real do usuário: sem a pausa, a segunda captura dispara
        antes de dar tempo de trocar a folha na mesa do scanner."""
        backend = _FakeBackend()
        _install_fake_backend(monkeypatch, backend)

        result = runner.invoke(
            app,
            ["scan-device", "--pages", "3", "--workers", "1", "--dry-run", "--json"],
            input="\n\n",  # Enter para as duas pausas (antes da página 2 e da 3)
        )

        assert result.exit_code == 0, result.output
        assert len(backend.capture_calls) == 3
        assert "Coloque a página 2 de 3" in result.output
        assert "Coloque a página 3 de 3" in result.output

    def test_all_pages_land_in_the_same_folder(self, monkeypatch: pytest.MonkeyPatch) -> None:
        backend = _FakeBackend()
        _install_fake_backend(monkeypatch, backend)

        runner.invoke(
            app, ["scan-device", "--pages", "2", "--dry-run", "--json"], input="\n"
        )

        assert len(backend.capture_calls) == 2
        assert backend.capture_calls[0]["output_dir"] == backend.capture_calls[1]["output_dir"]
