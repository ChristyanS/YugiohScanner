"""Testes de `images/grid.py` — detecção de grade de cartas (plano §22).

Fakeia `cv2` via `sys.modules` (mesmo padrão de `test_claude_provider.py` para
dependências pesadas e opcionais), para a suíte rápida não exigir o extra
`cv` instalado. `numpy` fica real — é dependência transitiva comum (rapidocr)
e `detect_grid_cells` só usa `np.array()`/`.shape`, nada que valha a pena
fakear.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
from PIL import Image, ImageDraw

from tests.factories import make_card_image
from yugioh_scanner.images.grid import (
    CvUnavailableError,
    InvalidGridSizeError,
    detect_grid_cells,
    ensure_cv_available,
    is_cell_blank,
    manual_grid_cells,
    parse_grid_size,
)
from yugioh_scanner.images.preprocess import BoundingBox


class _FakeCv2:
    """Contornos programados: cada item é `(x, y, w, h)` de um retângulo
    perfeito (`contourArea == w*h`, então retangularidade = 1.0)."""

    ADAPTIVE_THRESH_GAUSSIAN_C = 1
    THRESH_BINARY_INV = 1
    RETR_LIST = 1
    CHAIN_APPROX_SIMPLE = 1

    def __init__(self, boxes: list[tuple[int, int, int, int]]) -> None:
        self._boxes = boxes

    def GaussianBlur(self, array: Any, ksize: Any, sigma: Any) -> Any:  # noqa: N802
        return array

    def adaptiveThreshold(self, array: Any, *args: Any, **kwargs: Any) -> Any:  # noqa: N802
        return array

    def findContours(self, *_args: Any, **_kwargs: Any) -> tuple[list[int], None]:  # noqa: N802
        return list(range(len(self._boxes))), None

    def boundingRect(self, index: int) -> tuple[int, int, int, int]:  # noqa: N802
        return self._boxes[index]

    def contourArea(self, index: int) -> float:  # noqa: N802
        _, _, w, h = self._boxes[index]
        return float(w * h)


def _install_fake_cv2(
    monkeypatch: pytest.MonkeyPatch, boxes: list[tuple[int, int, int, int]]
) -> None:
    monkeypatch.setitem(sys.modules, "cv2", _FakeCv2(boxes))


def _image(size: tuple[int, int] = (600, 600)) -> Image.Image:
    return Image.new("RGB", size, color="white")


class TestEnsureCvAvailable:
    def test_raises_actionable_error_without_package(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # `sys.modules[name] = None` faz `import cv2` levantar `ImportError`
        # de propósito — não importa se o pacote está de fato instalado no
        # ambiente de quem roda os testes.
        monkeypatch.setitem(sys.modules, "cv2", None)
        with pytest.raises(CvUnavailableError) as exc_info:
            ensure_cv_available()
        assert "cv" in (exc_info.value.hint or "")

    def test_passes_when_package_present(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_fake_cv2(monkeypatch, [])
        ensure_cv_available()  # não levanta


class TestDetectGridCells:
    def test_small_image_returns_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_fake_cv2(monkeypatch, [(0, 0, 40, 58), (60, 0, 40, 58)])
        assert detect_grid_cells(_image((50, 50))) == []

    def test_returns_empty_with_fewer_than_two_candidates(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Uma caixa plausível só — não é grade, é o caminho de carta única.
        _install_fake_cv2(monkeypatch, [(50, 50, 200, 290)])
        assert detect_grid_cells(_image()) == []

    def test_returns_empty_above_max_cells(self, monkeypatch: pytest.MonkeyPatch) -> None:
        w, h = 100, 145  # aspecto ~0.69, dentro da tolerância de carta
        boxes = [(col * 120 + 10, row * 170 + 10, w, h) for row in range(4) for col in range(5)]
        _install_fake_cv2(monkeypatch, boxes)  # 20 candidatos > max_cells padrão (16)
        frame = (5 * 120 + 20, 4 * 170 + 20)
        assert detect_grid_cells(_image(frame), max_cells=16) == []

    def test_filters_out_boxes_with_wrong_aspect_ratio(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Quadrados não têm a proporção 59:86mm de uma carta — descartados.
        _install_fake_cv2(monkeypatch, [(0, 0, 100, 100), (200, 0, 100, 100), (0, 200, 100, 100)])
        assert detect_grid_cells(_image()) == []

    def test_detects_a_2x2_grid_in_reading_order(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Proporção 59:86mm. Colocadas fora de ordem de leitura de propósito,
        # para exercer o agrupamento por linha + ordenação esquerda→direita.
        w, h = 120, 175
        boxes = [
            (300, 300, w, h),  # baixo-direita
            (10, 10, w, h),  # cima-esquerda
            (10, 300, w, h),  # baixo-esquerda
            (300, 10, w, h),  # cima-direita
        ]
        _install_fake_cv2(monkeypatch, boxes)
        cells = detect_grid_cells(_image((600, 600)))

        assert len(cells) == 4
        # Ordem esperada: cima-esquerda, cima-direita, baixo-esquerda, baixo-direita.
        assert cells[0].left < cells[1].left
        assert cells[0].top == pytest.approx(cells[1].top, abs=0.02)
        assert cells[2].top > cells[0].top
        assert cells[2].left < cells[3].left

    def test_dedupes_nested_contours_of_the_same_card(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # cv2 sempre acha a borda interna e externa da mesma carta — só a
        # maior deve sobreviver.
        w, h = 120, 175
        outer = (10, 10, w, h)
        inner = (12, 12, w - 4, h - 4)
        second = (300, 10, w, h)
        third = (10, 300, w, h)
        _install_fake_cv2(monkeypatch, [outer, inner, second, third])

        cells = detect_grid_cells(_image((600, 600)))
        assert len(cells) == 3


@pytest.mark.ocr
class TestDetectGridCellsWithRealOpenCV:
    """Mesma função, sem fakear `cv2` — precisa do extra `cv` instalado
    (opt-in: `pytest -m ocr`). Fotos reais de grade ficam para um corpus
    dedicado quando existir (mesmo espírito de `tests/ocr/`); aqui só se
    verifica que o algoritmo de verdade acha uma grade óbvia e desenhada."""

    def test_detects_a_2x2_grid_of_drawn_cards(self) -> None:
        cell_w, cell_h = 120, 175  # proporção 59:86mm
        gutter = 40
        canvas = Image.new("RGB", (2 * cell_w + 3 * gutter, 2 * cell_h + 3 * gutter), "white")
        draw = ImageDraw.Draw(canvas)
        for row in range(2):
            for col in range(2):
                x = gutter + col * (cell_w + gutter)
                y = gutter + row * (cell_h + gutter)
                draw.rectangle(
                    [(x, y), (x + cell_w, y + cell_h)], fill=(200, 170, 90), outline="black", width=4
                )

        cells = detect_grid_cells(canvas)
        assert len(cells) == 4


class TestIsCellBlank:
    """Célula vazia de `--grid-size` (mesa do scanner sem carta) — plano §22:
    o layout informado pode ter mais células que cartas reais na página."""

    _FULL_CELL = BoundingBox(0.0, 0.0, 1.0, 1.0)

    def test_uniform_gray_cell_is_blank(self) -> None:
        image = Image.new("RGB", (300, 435), (245, 245, 245))
        assert is_cell_blank(image, self._FULL_CELL) is True

    def test_uniform_white_cell_is_blank(self) -> None:
        image = Image.new("RGB", (300, 435), "white")
        assert is_cell_blank(image, self._FULL_CELL) is True

    def test_synthetic_card_is_not_blank(self, tmp_path: Path) -> None:
        # Mesma carta sintética usada por `TestManualGridCells` — tem
        # moldura, faixas de nome/código e cores distintas: nem de longe lisa.
        path = make_card_image(tmp_path / "card.jpg")
        image = Image.open(path)
        assert is_cell_blank(image, self._FULL_CELL) is False

    def test_high_contrast_noise_is_not_blank(self) -> None:
        # Ruído xadrez: desvio-padrão e densidade de borda altos dos dois —
        # o oposto de uma mesa de scanner vazia.
        image = Image.new("RGB", (300, 435))
        draw = ImageDraw.Draw(image)
        for y in range(0, 435, 10):
            for x in range(0, 300, 10):
                if (x // 10 + y // 10) % 2 == 0:
                    draw.rectangle([(x, y), (x + 10, y + 10)], fill="black")
                else:
                    draw.rectangle([(x, y), (x + 10, y + 10)], fill="white")
        assert is_cell_blank(image, self._FULL_CELL) is False

    def test_degenerate_zero_area_box_is_never_blank(self) -> None:
        # Guarda defensiva: uma caixa inválida não deve ser tratada como
        # "vazia" (isso a descartaria silenciosamente do fan-out).
        image = Image.new("RGB", (300, 435), "white")
        assert is_cell_blank(image, BoundingBox(0.5, 0.5, 0.5, 0.5)) is False


class TestParseGridSize:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("3x3", (3, 3)),
            ("2X4", (2, 4)),
            (" 3 x 3 ", (3, 3)),
            ("1x1", (1, 1)),
        ],
    )
    def test_accepts_valid_formats(self, value: str, expected: tuple[int, int]) -> None:
        assert parse_grid_size(value) == expected

    @pytest.mark.parametrize("value", ["abc", "0x3", "3x0", "", "3x", "x3", "3.5x3"])
    def test_rejects_invalid_formats(self, value: str) -> None:
        with pytest.raises(InvalidGridSizeError) as exc_info:
            parse_grid_size(value)
        assert "LINHASxCOLUNAS" in (exc_info.value.hint or "")


class TestManualGridCells:
    def test_divides_the_detected_area_in_reading_order(self, tmp_path: Path) -> None:
        # `margin=120` é o mesmo parâmetro já provado em
        # tests/unit/test_preprocess.py para `detect_card_bounds` achar a
        # borda com confiança — reusado aqui para não reinventar a síntese
        # de imagem.
        path = make_card_image(tmp_path / "sheet.jpg", margin=120)
        image = Image.open(path)

        cells = manual_grid_cells(image, rows=3, cols=3)

        assert len(cells) == 9
        # Ordem de leitura: linha 0 (índices 0-2) antes da linha 1 (3-5).
        assert cells[0].top == pytest.approx(cells[1].top, abs=0.01)
        assert cells[1].top == pytest.approx(cells[2].top, abs=0.01)
        assert cells[3].top > cells[0].top
        assert cells[6].top > cells[3].top
        # Esquerda→direita dentro da linha.
        assert cells[0].left < cells[1].left < cells[2].left
        # A margem (moldura ao redor, plano §22) fica fora das células — se
        # `detect_card_bounds` não tivesse recortado, a primeira célula
        # começaria em 0.0.
        assert cells[0].left > 0.0
        assert cells[0].top > 0.0
        assert cells[-1].right < 1.0
        assert cells[-1].bottom < 1.0

    def test_falls_back_to_whole_image_when_no_bounds_detected(self) -> None:
        # Mesmo caso de `test_preprocess.py`: cor sólida não tem borda para
        # `detect_card_bounds` achar — devolve `None`, e a divisão usa a
        # foto inteira.
        image = Image.new("RGB", (600, 800), (128, 128, 128))

        cells = manual_grid_cells(image, rows=2, cols=2)

        assert len(cells) == 4
        assert cells[0].left == pytest.approx(0.0)
        assert cells[0].top == pytest.approx(0.0)
        assert cells[-1].right == pytest.approx(1.0)
        assert cells[-1].bottom == pytest.approx(1.0)

    def test_single_cell_covers_the_whole_detected_area(self, tmp_path: Path) -> None:
        path = make_card_image(tmp_path / "sheet.jpg", margin=120)
        image = Image.open(path)

        cells = manual_grid_cells(image, rows=1, cols=1)

        assert len(cells) == 1
        assert cells[0].left > 0.0  # ainda recorta a margem
