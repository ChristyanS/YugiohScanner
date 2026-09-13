"""Backend de captura WIA (plano §22, ADR 0011).

`win32com.client` é fakeado inteiro via `sys.modules` (mesma técnica de
`tests/unit/test_claude_provider.py` para o SDK da Anthropic) — nada aqui
precisa de hardware nem do pacote `pywin32` de verdade instalado.
"""

from __future__ import annotations

import io
import sys
import types
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from yugioh_scanner.capture import wia
from yugioh_scanner.errors import (
    NoScannerDeviceFoundError,
    ScanCaptureFailedError,
    ScannerUnavailableError,
    ScannerUnsupportedPlatformError,
)


class _FakeProperty:
    def __init__(self, value: Any = None) -> None:
        self.Value = value


class _FakePropertyBag:
    """`item.Properties(id_or_name)` — WIA expõe propriedades como uma
    coleção chamável, não um dict comum."""

    def __init__(self, **named: Any) -> None:
        self._props: dict[Any, _FakeProperty] = {
            key: _FakeProperty(value) for key, value in named.items()
        }

    def __call__(self, key: Any) -> _FakeProperty:
        return self._props.setdefault(key, _FakeProperty())


class _FakeDeviceInfo:
    def __init__(self, device_id: str, name: str, device: _FakeDevice | None = None) -> None:
        self.DeviceID = device_id
        self.Properties = _FakePropertyBag(Name=name)
        self._device = device

    def Connect(self) -> _FakeDevice:  # noqa: N802
        assert self._device is not None
        return self._device


class _FakeItem:
    def __init__(self, image: Any = None, transfer_error: Exception | None = None) -> None:
        self.Properties = _FakePropertyBag()
        self._image = image
        self._transfer_error = transfer_error

    def Transfer(self, _format_guid: str) -> Any:  # noqa: N802
        if self._transfer_error is not None:
            raise self._transfer_error
        return self._image


class _FakeDevice:
    def __init__(self, item: _FakeItem) -> None:
        self.Items = {1: item}


class _FakeImage:
    def __init__(self, *, real_format: str = "BMP") -> None:
        self.saved_to: str | None = None
        #: BMP por padrão de propósito: mimetiza o driver real (Canon
        #: G3010) que ignora o GUID de formato pedido e devolve BMP mesmo
        #: assim — é o bug que `capture()` precisa normalizar, não confiar
        #: cegamente na extensão `.jpg`.
        self._real_format = real_format

    def SaveFile(self, path: str) -> None:  # noqa: N802
        self.saved_to = path
        buffer = io.BytesIO()
        Image.new("RGB", (4, 4), "white").save(buffer, self._real_format)
        Path(path).write_bytes(buffer.getvalue())


class _FakeDeviceManager:
    def __init__(self, device_infos: list[_FakeDeviceInfo]) -> None:
        self.DeviceInfos = device_infos


def _install_fake_win32com(monkeypatch: pytest.MonkeyPatch, manager: _FakeDeviceManager) -> None:
    win32com_module = types.ModuleType("win32com")
    client_module = types.ModuleType("win32com.client")
    client_module.Dispatch = lambda _prog_id: manager  # type: ignore[attr-defined]
    win32com_module.client = client_module  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "win32com", win32com_module)
    monkeypatch.setitem(sys.modules, "win32com.client", client_module)
    # `pythoncom.CoInitialize()` é chamado antes de todo `Dispatch()` (init
    # de COM por thread, plano §22/ADR 0011) — fakeado junto, senão o teste
    # exigiria `pywin32` de verdade instalado só para essa chamada.
    pythoncom_module = types.ModuleType("pythoncom")
    pythoncom_module.CoInitialize = lambda: None  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pythoncom", pythoncom_module)


class TestBuild:
    def test_raises_on_non_windows(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(wia.platform, "system", lambda: "Linux")
        with pytest.raises(ScannerUnsupportedPlatformError):
            wia.build()

    def test_raises_without_pywin32(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(wia.platform, "system", lambda: "Windows")
        # Força `import win32com.client` a levantar `ImportError`, não importa
        # se o pacote está de fato instalado em quem roda o teste.
        monkeypatch.setitem(sys.modules, "win32com", None)
        with pytest.raises(ScannerUnavailableError):
            wia.build()

    def test_succeeds_on_windows_with_pywin32(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(wia.platform, "system", lambda: "Windows")
        _install_fake_win32com(monkeypatch, _FakeDeviceManager([]))
        assert isinstance(wia.build(), wia.WiaScannerBackend)


class TestListDevices:
    def test_returns_device_infos(self, monkeypatch: pytest.MonkeyPatch) -> None:
        manager = _FakeDeviceManager(
            [_FakeDeviceInfo("dev-1", "Scanner A"), _FakeDeviceInfo("dev-2", "Scanner B")]
        )
        _install_fake_win32com(monkeypatch, manager)

        devices = wia.WiaScannerBackend().list_devices()

        assert [d.id for d in devices] == ["dev-1", "dev-2"]
        assert [d.name for d in devices] == ["Scanner A", "Scanner B"]

    def test_no_devices_returns_empty_list(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_fake_win32com(monkeypatch, _FakeDeviceManager([]))
        assert wia.WiaScannerBackend().list_devices() == []

    def test_com_error_becomes_a_domain_error_not_a_raw_exception(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regressão: um erro de COM cru (ex.: thread do pool da Web sem
        `CoInitialize`, achado real) não pode vazar como exceção genérica —
        vira `ScanCaptureFailedError` com mensagem, não um 500 opaco que o
        JS de `/scan` trata como "sem scanner" e esconde em silêncio."""

        def broken_dispatch(_prog_id: str) -> None:
            raise RuntimeError("CoInitialize not called on this thread")

        win32com_module = types.ModuleType("win32com")
        client_module = types.ModuleType("win32com.client")
        client_module.Dispatch = broken_dispatch  # type: ignore[attr-defined]
        win32com_module.client = client_module  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "win32com", win32com_module)
        monkeypatch.setitem(sys.modules, "win32com.client", client_module)
        pythoncom_module = types.ModuleType("pythoncom")
        pythoncom_module.CoInitialize = lambda: None  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "pythoncom", pythoncom_module)

        with pytest.raises(ScanCaptureFailedError):
            wia.WiaScannerBackend().list_devices()


class TestCapture:
    def test_raises_when_no_device_found(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _install_fake_win32com(monkeypatch, _FakeDeviceManager([]))
        with pytest.raises(NoScannerDeviceFoundError):
            wia.WiaScannerBackend().capture(tmp_path)

    def test_raises_when_requested_device_id_is_unknown(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        device = _FakeDevice(_FakeItem(image=_FakeImage()))
        manager = _FakeDeviceManager([_FakeDeviceInfo("dev-1", "Scanner A", device)])
        _install_fake_win32com(monkeypatch, manager)

        with pytest.raises(NoScannerDeviceFoundError):
            wia.WiaScannerBackend().capture(tmp_path, device_id="dev-inexistente")

    def test_saves_file_and_returns_path(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        image = _FakeImage()
        device = _FakeDevice(_FakeItem(image=image))
        manager = _FakeDeviceManager([_FakeDeviceInfo("dev-1", "Scanner A", device)])
        _install_fake_win32com(monkeypatch, manager)

        result = wia.WiaScannerBackend().capture(tmp_path, dpi=600, color_mode="gray")

        assert result.parent == tmp_path
        assert result.name == "capture-1.jpg"
        assert image.saved_to is not None
        # O arquivo intermediário (bruto, no formato que o driver de fato
        # devolveu) não sobra no disco depois da normalização.
        assert not Path(image.saved_to).exists()

    def test_normalizes_driver_format_mismatch_to_real_jpeg(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Regressão: um Canon G3010 real devolve BMP mesmo quando JPEG é
        pedido via `wiaFormatJPEG` — salvar isso direto como `.jpg` produz
        bytes que não batem com a extensão, e `looks_like_image()` rejeita a
        foto inteira como inválida rio abaixo (achado real, não hipotético).
        """
        device = _FakeDevice(_FakeItem(image=_FakeImage(real_format="BMP")))
        manager = _FakeDeviceManager([_FakeDeviceInfo("dev-1", "Scanner A", device)])
        _install_fake_win32com(monkeypatch, manager)

        result = wia.WiaScannerBackend().capture(tmp_path)

        assert result.suffix == ".jpg"
        with Image.open(result) as saved:
            saved.load()
            assert saved.format == "JPEG"

    def test_selects_device_by_id(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        wrong_image = _FakeImage()
        right_image = _FakeImage()
        manager = _FakeDeviceManager(
            [
                _FakeDeviceInfo("dev-1", "Errado", _FakeDevice(_FakeItem(image=wrong_image))),
                _FakeDeviceInfo("dev-2", "Certo", _FakeDevice(_FakeItem(image=right_image))),
            ]
        )
        _install_fake_win32com(monkeypatch, manager)

        wia.WiaScannerBackend().capture(tmp_path, device_id="dev-2")

        assert right_image.saved_to is not None
        assert wrong_image.saved_to is None

    def test_wraps_com_errors(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        device = _FakeDevice(_FakeItem(transfer_error=RuntimeError("com error")))
        manager = _FakeDeviceManager([_FakeDeviceInfo("dev-1", "Scanner A", device)])
        _install_fake_win32com(monkeypatch, manager)

        with pytest.raises(ScanCaptureFailedError):
            wia.WiaScannerBackend().capture(tmp_path)

    def test_numbers_successive_captures(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        device = _FakeDevice(_FakeItem(image=_FakeImage()))
        manager = _FakeDeviceManager([_FakeDeviceInfo("dev-1", "Scanner A", device)])
        _install_fake_win32com(monkeypatch, manager)
        backend = wia.WiaScannerBackend()

        first = backend.capture(tmp_path)
        second = backend.capture(tmp_path)

        assert first.name == "capture-1.jpg"
        assert second.name == "capture-2.jpg"
