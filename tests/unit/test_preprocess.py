"""Pré-processamento de imagens (plano §5.2)."""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from tests.factories import make_card_image, make_corrupted_image, make_truncated_jpeg
from yugioh_scanner.images.preprocess import (
    CODE_ROI,
    NAME_ROI,
    BoundingBox,
    ImageError,
    detect_card_bounds,
    downscale,
    enhance_for_ocr,
    has_supported_extension,
    load_image,
    looks_like_image,
    prepare_image,
)

MAX_PIXELS = 40_000_000


class TestExtensionAndMagicBytes:
    @pytest.mark.parametrize("name", ["a.jpg", "a.JPG", "a.jpeg", "a.png", "a.PNG"])
    def test_supported(self, name: str) -> None:
        assert has_supported_extension(Path(name))

    @pytest.mark.parametrize("name", ["a.gif", "a.webp", "a.pdf", "a.txt", "a"])
    def test_unsupported(self, name: str) -> None:
        assert not has_supported_extension(Path(name))

    def test_extension_alone_is_not_trusted(self, tmp_path: Path) -> None:
        """A extensão é entrada do usuário; os bytes iniciais não mentem."""
        fake = make_corrupted_image(tmp_path / "mentiroso.jpg")
        assert has_supported_extension(fake)
        assert not looks_like_image(fake)

    def test_real_image_passes(self, tmp_path: Path) -> None:
        assert looks_like_image(make_card_image(tmp_path / "ok.jpg"))


class TestLoadImage:
    def test_loads_a_valid_card(self, tmp_path: Path) -> None:
        image = load_image(make_card_image(tmp_path / "c.jpg"), max_pixels=MAX_PIXELS)
        assert image.mode == "RGB"
        assert image.height > image.width, "carta é mais alta que larga"

    def test_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(ImageError, match="não encontrado"):
            load_image(tmp_path / "nao_existe.jpg", max_pixels=MAX_PIXELS)

    def test_unsupported_extension(self, tmp_path: Path) -> None:
        path = tmp_path / "x.gif"
        path.write_bytes(b"GIF89a")
        with pytest.raises(ImageError, match="Extensão"):
            load_image(path, max_pixels=MAX_PIXELS)

    def test_garbage_content(self, tmp_path: Path) -> None:
        with pytest.raises(ImageError, match="não é um JPEG/PNG"):
            load_image(make_corrupted_image(tmp_path / "lixo.jpg"), max_pixels=MAX_PIXELS)

    def test_truncated_jpeg(self, tmp_path: Path) -> None:
        """Cabeçalho válido, corpo cortado — o `verify()` precisa pegar."""
        with pytest.raises(ImageError):
            load_image(make_truncated_jpeg(tmp_path / "meio.jpg"), max_pixels=MAX_PIXELS)

    def test_decompression_bomb_guard(self, tmp_path: Path) -> None:
        """Limite de pixels protege a RAM (plano §21)."""
        path = make_card_image(tmp_path / "grande.jpg", height=400)
        with pytest.raises(ImageError, match="acima do limite"):
            load_image(path, max_pixels=1000)

    def test_exif_orientation_is_applied(self, tmp_path: Path) -> None:
        """Foto de celular chega girada; sem corrigir, o OCR erra tudo."""
        path = tmp_path / "girada.jpg"
        source = Image.new("RGB", (200, 100), (255, 0, 0))
        # EXIF orientation 6 = girar 90° no sentido horário ao exibir.
        exif = source.getexif()
        exif[274] = 6
        source.save(path, exif=exif)
        source.close()

        loaded = load_image(path, max_pixels=MAX_PIXELS)
        assert loaded.size == (100, 200), "a rotação do EXIF não foi aplicada"


class TestDownscale:
    def test_shrinks_large_images(self, tmp_path: Path) -> None:
        image = Image.new("RGB", (4000, 3000))
        result = downscale(image, max_dimension=1600)
        assert max(result.size) == 1600
        assert result.size == (1600, 1200), "a proporção precisa ser mantida"

    def test_leaves_small_images_untouched(self) -> None:
        image = Image.new("RGB", (800, 600))
        assert downscale(image, max_dimension=1600) is image


class TestRegions:
    def test_rois_are_disjoint_and_in_the_right_halves(self) -> None:
        """Nome em cima, código logo abaixo da arte — nem no topo, nem perto
        do copyright (posição antiga, nunca conferida contra foto real:
        media a caixa de efeito, 0% de acerto no corpus real de fotos)."""
        assert NAME_ROI.bottom < CODE_ROI.top
        assert NAME_ROI.top < 0.2
        assert 0.6 < CODE_ROI.top < 0.8, "abaixo da arte, bem antes do rodapé"
        assert CODE_ROI.bottom < 0.85, "acima da caixa de texto/efeito"

    def test_to_pixels_clamps_to_the_image(self) -> None:
        box = BoundingBox(0.0, 0.0, 1.5, 1.5)
        assert box.to_pixels(100, 200) == (0, 0, 100, 200)

    def test_prepare_produces_the_three_regions(self, tmp_path: Path) -> None:
        prepared = prepare_image(make_card_image(tmp_path / "c.jpg"), max_pixels=MAX_PIXELS)
        try:
            assert set(prepared.regions) == {"name", "code", "full"}
            assert all(image.mode == "L" for image in prepared.regions.values())
        finally:
            prepared.close()

    def test_code_region_is_upscaled(self, tmp_path: Path) -> None:
        """O set code é o menor texto da carta: sem ampliar, o OCR não lê."""
        prepared = prepare_image(make_card_image(tmp_path / "c.jpg"), max_pixels=MAX_PIXELS)
        try:
            width = prepared.width
            expected_raw = int((CODE_ROI.right - CODE_ROI.left) * width)
            assert prepared.regions["code"].width > expected_raw
        finally:
            prepared.close()

    def test_notes_record_what_happened(self, tmp_path: Path) -> None:
        prepared = prepare_image(make_card_image(tmp_path / "c.jpg"), max_pixels=MAX_PIXELS)
        try:
            assert "original_size" in prepared.notes
            assert "working_size" in prepared.notes
        finally:
            prepared.close()


class TestCardDetection:
    def test_finds_the_card_inside_a_photo_with_background(self, tmp_path: Path) -> None:
        path = make_card_image(tmp_path / "com_fundo.jpg", margin=120)
        image = load_image(path, max_pixels=MAX_PIXELS)
        bounds = detect_card_bounds(image)
        assert bounds is not None
        left, top, right, bottom = bounds
        # A caixa encontrada deve ser bem menor que a foto inteira.
        assert (right - left) < image.width
        assert (bottom - top) < image.height

    def test_returns_none_for_uniform_images(self) -> None:
        """Sem bordas não há o que detectar — melhor admitir do que chutar."""
        assert detect_card_bounds(Image.new("RGB", (600, 800), (128, 128, 128))) is None

    def test_returns_none_for_tiny_images(self) -> None:
        assert detect_card_bounds(Image.new("RGB", (10, 10))) is None

    def test_bad_detection_falls_back_instead_of_cropping_wrong(self, tmp_path: Path) -> None:
        """Recortar errado desloca as ROIs e estraga as duas leituras."""
        path = make_card_image(tmp_path / "c.jpg")
        prepared = prepare_image(path, max_pixels=MAX_PIXELS, auto_crop=True)
        try:
            assert "cropped_to" in prepared.notes
            assert prepared.regions["name"].width > 0
        finally:
            prepared.close()


class TestEnhance:
    def test_converts_to_grayscale(self) -> None:
        assert enhance_for_ocr(Image.new("RGB", (100, 50), (200, 30, 30))).mode == "L"

    def test_upscale_multiplies_dimensions(self) -> None:
        result = enhance_for_ocr(Image.new("RGB", (100, 50)), upscale=2)
        assert result.size == (200, 100)
