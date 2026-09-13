"""Captura de páginas de scanner/impressora físico (plano §22, ADR 0011).

Não confundir com `scanner/`: aquele pacote é a orquestração do OCR
(descoberta, paralelismo, worker) sobre arquivos já em disco. Este pacote
resolve só a etapa anterior — "como um arquivo chega no disco" — e devolve o
resto do trabalho para `services.scan_service.ScanService.scan()`, sem
duplicar nenhuma lógica de identificação.
"""

from __future__ import annotations

from .base import ScannerBackend, ScannerDeviceInfo
from .registry import create_backend, known_backends

__all__ = [
    "ScannerBackend",
    "ScannerDeviceInfo",
    "create_backend",
    "known_backends",
]
