"""Backend de captura via WIA (Windows Image Acquisition).

Usa automação COM (`win32com.client`) contra `WIA.DeviceManager`, não
`WIA.CommonDialog` — o `CommonDialog` abre a caixa de seleção nativa do
Windows, o que exigiria um usuário clicando; `DeviceManager` é scriptável e
funciona sem interação, o que a CLI precisa.

Os IDs de propriedade WIA abaixo (`_WIA_PROP_*`) são os padrão documentados
pela Microsoft (namespace `WIA_IPS_*` / `WIA_DIP_*`) e não variam entre
drivers — o que varia por driver é a *faixa aceita* de cada valor (DPI
mínimo/máximo, por exemplo) e a semântica de `Items` num scanner com
alimentador automático (ADF) vs. mesa plana. Isto foi implementado contra a
documentação oficial, não contra hardware real — a primeira captura contra um
scanner de verdade é o smoke test que confirma os detalhes (ADR 0011).
"""

from __future__ import annotations

import platform
import uuid
from pathlib import Path
from typing import Any

from PIL import Image

from ..errors import (
    NoScannerDeviceFoundError,
    ScanCaptureFailedError,
    ScannerUnavailableError,
    ScannerUnsupportedPlatformError,
)
from .base import ScannerDeviceInfo

#: WIA_IPS_CUR_INTENT / WIA_IPS_XRES / WIA_IPS_YRES — propriedades padrão de
#: qualquer item WIA, documentadas pela Microsoft.
_PROP_CUR_INTENT = "6146"
_PROP_XRES = "6147"
_PROP_YRES = "6148"

#: WIA_INTENT_IMAGE_TYPE_COLOR / _GRAYSCALE.
_INTENT_COLOR = 1
_INTENT_GRAYSCALE = 2

#: `wiaFormatJPEG` — GUID padrão do formato de saída JPEG no WIA.
_WIA_FORMAT_JPEG = "{B96B3CAE-0728-11D3-9D7B-0000F81EF32E}"

#: Mesa plana: primeiro (e geralmente único) item de um `Device.Items`. Um
#: scanner com alimentador automático (ADF) tem semântica própria aqui — fora
#: de escopo desta primeira versão (ADR 0011).
_FLATBED_ITEM_INDEX = 1


def build() -> WiaScannerBackend:
    if platform.system() != "Windows":
        raise ScannerUnsupportedPlatformError()
    try:
        import win32com.client  # noqa: F401
    except ImportError as exc:
        raise ScannerUnavailableError() from exc
    return WiaScannerBackend()


class WiaScannerBackend:
    def _device_manager(self) -> Any:
        import pythoncom
        import win32com.client

        # COM precisa ser inicializado **por thread**, não por processo. A
        # Web roda cada rota síncrona (`list_devices`/`capture`) numa thread
        # de um pool (FastAPI/anyio) que é reciclado com o tempo — a thread
        # que atendeu a primeira chamada pode não ser a mesma que atende a
        # próxima. Sem isto, `Dispatch()` falha nessa segunda thread com um
        # erro de COM cru (achado real: a seção de captura em `/scan`
        # funcionava ao carregar a página, e sumia silenciosamente depois de
        # navegar para `/review` e voltar — tempo suficiente para o pool
        # trocar de thread). `CoInitialize()` é seguro de chamar de novo na
        # mesma thread (devolve `S_FALSE`, não levanta).
        pythoncom.CoInitialize()
        return win32com.client.Dispatch("WIA.DeviceManager")

    def list_devices(self) -> list[ScannerDeviceInfo]:
        try:
            manager = self._device_manager()
            return [
                ScannerDeviceInfo(id=info.DeviceID, name=info.Properties("Name").Value)
                for info in manager.DeviceInfos
            ]
        except Exception as exc:  # COM levanta pywintypes.com_error genérico
            # Sem isto, um erro de COM cru sobe até a rota Web como 500 sem
            # `user_message`/`hint` — o JS de `/scan` trata qualquer falha
            # aqui como "sem scanner" e some com a seção em silêncio, o que
            # esconde o problema de verdade em vez de dar um erro acionável.
            raise ScanCaptureFailedError(str(exc)) from exc

    def capture(
        self,
        output_dir: Path,
        *,
        device_id: str | None = None,
        dpi: int = 300,
        color_mode: str = "color",
    ) -> Path:
        manager = self._device_manager()
        infos = list(manager.DeviceInfos)
        if not infos:
            raise NoScannerDeviceFoundError()

        target_info = infos[0]
        if device_id is not None:
            for info in infos:
                if info.DeviceID == device_id:
                    target_info = info
                    break
            else:
                raise NoScannerDeviceFoundError()

        try:
            device = target_info.Connect()
            item = device.Items[_FLATBED_ITEM_INDEX]
            intent = _INTENT_COLOR if color_mode == "color" else _INTENT_GRAYSCALE
            item.Properties(_PROP_CUR_INTENT).Value = intent
            item.Properties(_PROP_XRES).Value = dpi
            item.Properties(_PROP_YRES).Value = dpi
            image = item.Transfer(_WIA_FORMAT_JPEG)
        except Exception as exc:  # COM levanta pywintypes.com_error genérico
            raise ScanCaptureFailedError(str(exc)) from exc

        output_dir.mkdir(parents=True, exist_ok=True)
        # `_WIA_FORMAT_JPEG` é só um pedido — muitos drivers (confirmado com
        # um Canon G3010 real) o ignoram e devolvem BMP de qualquer jeito.
        # Salvar isso direto como `.jpg` produz um arquivo cujos bytes não
        # batem com a extensão, e `looks_like_image()` rejeita como
        # inválido rio abaixo. Em vez de confiar no driver, normaliza
        # sempre: salva o que veio, deixa o Pillow detectar o formato de
        # verdade pelos bytes, e regrava como JPEG.
        raw_path = output_dir / f"_raw_{uuid.uuid4().hex}"
        try:
            image.SaveFile(str(raw_path))
            target_path = output_dir / f"capture-{_next_index(output_dir)}.jpg"
            with Image.open(raw_path) as raw_image:
                raw_image.convert("RGB").save(target_path, "JPEG", quality=95)
        except OSError as exc:
            raise ScanCaptureFailedError(f"Falha ao normalizar a imagem capturada: {exc}") from exc
        finally:
            raw_path.unlink(missing_ok=True)
        return target_path


def _next_index(output_dir: Path) -> int:
    existing = list(output_dir.glob("capture-*.jpg"))
    return len(existing) + 1
