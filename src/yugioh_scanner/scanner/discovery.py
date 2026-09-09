"""Descoberta de imagens em uma pasta (plano §13.1 e §21).

Duas responsabilidades, ambas com consequência real:

**Identidade por conteúdo.** Cada arquivo é identificado pelo SHA-256 do seu
conteúdo, não pelo caminho. É isso que faz `scan` ser idempotente mesmo depois
de renomear ou mover as fotos.

**Guarda de caminho.** A pasta vem do usuário (e, na Fase 8, do navegador).
Resolvemos o caminho real antes de qualquer coisa e, se houver allowlist
configurada, exigimos que ele esteja dentro dela.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from ..config import Settings
from ..errors import InvalidScanPathError
from ..images.preprocess import SUPPORTED_EXTENSIONS, has_supported_extension

#: Blocos de 64 KB: hashear um arquivo grande não pode carregar tudo em RAM.
_HASH_CHUNK = 64 * 1024


@dataclass(frozen=True, slots=True)
class DiscoveredImage:
    """Um arquivo candidato, já identificado pelo conteúdo."""

    path: Path
    size: int
    file_hash: str

    @property
    def name(self) -> str:
        return self.path.name


def hash_file(path: Path) -> str:
    """SHA-256 do conteúdo, em streaming."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_HASH_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_scan_folder(folder: Path | str, settings: Settings) -> Path:
    """Valida a pasta pedida e devolve o caminho real.

    `resolve()` acontece **antes** da checagem da allowlist — é o que impede
    que `raiz/../../etc` passe pela verificação (plano §21).
    """
    try:
        resolved = Path(folder).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise InvalidScanPathError(f"Pasta não encontrada: {folder}") from exc

    if not resolved.is_dir():
        raise InvalidScanPathError(f"Não é uma pasta: {resolved}")

    roots = settings.allowed_scan_roots
    if roots and not any(_is_within(resolved, root) for root in roots):
        raise InvalidScanPathError(
            f"A pasta {resolved} está fora das raízes permitidas.",
            hint="Ajuste YGS_ALLOWED_SCAN_ROOTS ou escaneie uma pasta permitida.",
        )
    return resolved


def _is_within(path: Path, root: Path) -> bool:
    try:
        return path == root or path.is_relative_to(root)
    except ValueError:  # pragma: no cover - caminhos em drives diferentes
        return False


def iter_image_paths(folder: Path, *, recursive: bool = False) -> Iterator[Path]:
    """Percorre a pasta devolvendo os arquivos com extensão suportada.

    Ordenado por caminho: um scan reproduzível facilita comparar duas execuções.
    """
    pattern = "**/*" if recursive else "*"
    for path in sorted(folder.glob(pattern)):
        if path.is_file() and has_supported_extension(path):
            yield path


def discover_images(
    folder: Path,
    *,
    recursive: bool = False,
    limit: int | None = None,
) -> list[DiscoveredImage]:
    """Lista e hasheia as imagens da pasta.

    Arquivos ilegíveis são simplesmente omitidos aqui: sem hash não há como
    identificá-los, e o erro real aparece no pré-processamento, onde há uma
    `ScanImage` para registrá-lo.
    """
    found: list[DiscoveredImage] = []
    for path in iter_image_paths(folder, recursive=recursive):
        try:
            stat = path.stat()
            found.append(DiscoveredImage(path=path, size=stat.st_size, file_hash=hash_file(path)))
        except OSError:
            continue
        if limit is not None and len(found) >= limit:
            break
    return found


def describe_supported() -> str:
    return ", ".join(sorted(SUPPORTED_EXTENSIONS))
