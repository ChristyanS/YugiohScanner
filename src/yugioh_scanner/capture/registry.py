"""Registro de backends de captura (plano §22), no mesmo espírito de
`ocr/registry.py`: import preguiçoso, e quem falta diz qual extra instalar.
"""

from __future__ import annotations

from importlib import import_module

from ..errors import ScannerBackendUnknownError
from .base import ScannerBackend

#: nome → módulo com uma função `build() -> ScannerBackend`
_REGISTRY: dict[str, str] = {
    "wia": "yugioh_scanner.capture.wia",
}


def known_backends() -> list[str]:
    return sorted(_REGISTRY)


def create_backend(name: str) -> ScannerBackend:
    """Instancia um backend de captura pelo nome.

    Levanta cedo (não só no primeiro `capture()`) se a plataforma ou a
    dependência não derem: `--list-devices`/`scan-device` devem falhar com um
    erro acionável antes de qualquer tentativa de usar o hardware.
    """
    module_path = _REGISTRY.get(name)
    if module_path is None:
        raise ScannerBackendUnknownError(name, known_backends())

    module = import_module(module_path)
    build = module.build
    return build()
