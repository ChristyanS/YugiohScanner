"""Contrato de captura (plano §22) — o ponto de desacoplamento do hardware.

Deliberadamente magro: só o que o backend WIA de hoje precisa. Não é uma
abstração especulativa para SANE/TWAIN, que ainda não existem no projeto — a
interface cresce quando um segundo backend de verdade justificar (ADR 0011).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class ScannerDeviceInfo:
    """Um dispositivo WIA encontrado no sistema."""

    id: str
    name: str


@runtime_checkable
class ScannerBackend(Protocol):
    """Uma fonte de captura de página física."""

    def list_devices(self) -> list[ScannerDeviceInfo]:
        """Dispositivos disponíveis agora. `[]` se nenhum for encontrado."""
        ...

    def capture(
        self,
        output_dir: Path,
        *,
        device_id: str | None = None,
        dpi: int = 300,
        color_mode: str = "color",
    ) -> Path:
        """Captura uma página e devolve o caminho do arquivo salvo em `output_dir`.

        `device_id=None` usa o primeiro dispositivo encontrado — o caso comum
        de quem só tem um scanner instalado.
        """
        ...
