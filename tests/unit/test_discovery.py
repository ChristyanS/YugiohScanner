"""Descoberta de imagens e guarda de caminho (plano §13.1 e §21)."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.factories import make_card_image, make_corrupted_image
from yugioh_scanner.config import Settings
from yugioh_scanner.errors import InvalidScanPathError
from yugioh_scanner.scanner.discovery import (
    discover_images,
    hash_file,
    iter_image_paths,
    resolve_scan_folder,
)


@pytest.fixture
def cards_folder(tmp_path: Path) -> Path:
    folder = tmp_path / "cards"
    make_card_image(folder / "IMG_001.jpg", name="BLUE-EYES WHITE DRAGON")
    make_card_image(folder / "IMG_002.jpeg", name="DARK MAGICIAN")
    make_card_image(folder / "IMG_003.png", name="POT OF GREED")
    (folder / "anotacoes.txt").write_text("ignorar", encoding="utf-8")
    make_card_image(folder / "sub" / "IMG_004.jpg", name="KURIBOH")
    return folder


class TestIteration:
    def test_finds_only_supported_extensions(self, cards_folder: Path) -> None:
        names = [path.name for path in iter_image_paths(cards_folder)]
        assert names == ["IMG_001.jpg", "IMG_002.jpeg", "IMG_003.png"]

    def test_ignores_subfolders_by_default(self, cards_folder: Path) -> None:
        assert "IMG_004.jpg" not in [p.name for p in iter_image_paths(cards_folder)]

    def test_recursive_includes_subfolders(self, cards_folder: Path) -> None:
        names = {path.name for path in iter_image_paths(cards_folder, recursive=True)}
        assert "IMG_004.jpg" in names

    def test_order_is_deterministic(self, cards_folder: Path) -> None:
        """Scan reproduzível: comparar duas execuções tem de ser possível."""
        first = list(iter_image_paths(cards_folder))
        second = list(iter_image_paths(cards_folder))
        assert first == second == sorted(first)


class TestHashing:
    def test_same_content_same_hash(self, tmp_path: Path) -> None:
        a = make_card_image(tmp_path / "a.jpg", name="X", set_code="LOB-001")
        b = tmp_path / "b.jpg"
        b.write_bytes(a.read_bytes())
        assert hash_file(a) == hash_file(b)

    def test_different_content_different_hash(self, tmp_path: Path) -> None:
        a = make_card_image(tmp_path / "a.jpg", name="BLUE-EYES")
        b = make_card_image(tmp_path / "b.jpg", name="DARK MAGICIAN")
        assert hash_file(a) != hash_file(b)

    def test_hash_is_content_based_not_path_based(self, tmp_path: Path) -> None:
        """É isto que faz o `scan` sobreviver a renomear e mover arquivos."""
        original = make_card_image(tmp_path / "IMG_001.jpg")
        digest = hash_file(original)
        renamed = original.rename(tmp_path / "blue_eyes.jpg")
        assert hash_file(renamed) == digest

    def test_hash_is_hex_sha256(self, tmp_path: Path) -> None:
        digest = hash_file(make_card_image(tmp_path / "a.jpg"))
        assert len(digest) == 64
        assert all(char in "0123456789abcdef" for char in digest)


class TestDiscoverImages:
    def test_returns_size_and_hash(self, cards_folder: Path) -> None:
        images = discover_images(cards_folder)
        assert len(images) == 3
        assert all(image.size > 0 and len(image.file_hash) == 64 for image in images)

    def test_limit(self, cards_folder: Path) -> None:
        assert len(discover_images(cards_folder, limit=2)) == 2

    def test_recursive(self, cards_folder: Path) -> None:
        assert len(discover_images(cards_folder, recursive=True)) == 4

    def test_empty_folder(self, tmp_path: Path) -> None:
        empty = tmp_path / "vazia"
        empty.mkdir()
        assert discover_images(empty) == []

    def test_corrupted_file_is_still_discovered(self, tmp_path: Path) -> None:
        """A descoberta não valida conteúdo: o erro pertence ao pipeline, que
        tem uma `ScanImage` para registrá-lo."""
        folder = tmp_path / "cards"
        make_corrupted_image(folder / "quebrada.jpg")
        assert len(discover_images(folder)) == 1


class TestPathGuard:
    def test_accepts_a_normal_folder(self, cards_folder: Path, settings: Settings) -> None:
        assert resolve_scan_folder(cards_folder, settings) == cards_folder.resolve()

    def test_rejects_missing_folder(self, tmp_path: Path, settings: Settings) -> None:
        with pytest.raises(InvalidScanPathError, match="não encontrada"):
            resolve_scan_folder(tmp_path / "nao_existe", settings)

    def test_rejects_a_file(self, tmp_path: Path, settings: Settings) -> None:
        path = make_card_image(tmp_path / "a.jpg")
        with pytest.raises(InvalidScanPathError, match="Não é uma pasta"):
            resolve_scan_folder(path, settings)

    def test_allowlist_permits_folders_inside_it(self, tmp_path: Path, cards_folder: Path) -> None:
        settings = Settings(data_path=tmp_path / "data", allowed_scan_roots=[tmp_path])
        assert resolve_scan_folder(cards_folder, settings) == cards_folder.resolve()

    def test_allowlist_blocks_folders_outside_it(self, tmp_path: Path, cards_folder: Path) -> None:
        outside = tmp_path / "outro"
        outside.mkdir()
        settings = Settings(data_path=tmp_path / "data", allowed_scan_roots=[outside])
        with pytest.raises(InvalidScanPathError, match="fora das raízes"):
            resolve_scan_folder(cards_folder, settings)

    def test_traversal_is_resolved_before_the_check(self, tmp_path: Path) -> None:
        """`raiz/../fora` precisa ser resolvido **antes** de validar (§21).

        Sem isso, a allowlist é contornável com dois pontinhos.
        """
        allowed = tmp_path / "permitida"
        allowed.mkdir()
        forbidden = tmp_path / "proibida"
        forbidden.mkdir()

        settings = Settings(data_path=tmp_path / "data", allowed_scan_roots=[allowed])
        with pytest.raises(InvalidScanPathError):
            resolve_scan_folder(allowed / ".." / "proibida", settings)

    def test_empty_allowlist_permits_everything(
        self, cards_folder: Path, settings: Settings
    ) -> None:
        """Modo local (default): sem allowlist configurada, qualquer pasta vale."""
        assert settings.allowed_scan_roots == []
        assert resolve_scan_folder(cards_folder, settings).is_dir()
