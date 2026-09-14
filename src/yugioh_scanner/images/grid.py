"""Detecção de grade de cartas numa única foto (plano §22).

Mesma filosofia de `detect_card_bounds` (`images/preprocess.py`): **nunca
chuta**. Se não há confiança de que a foto contém uma grade de cartas,
`detect_grid_cells` devolve `[]` e quem chama trata a foto como carta única —
o caminho de hoje, inalterado. Recortar errado é pior do que não recortar: uma
grade mal detectada estraga N leituras de OCR em vez de uma.

Depende de OpenCV (`cv2`) + `numpy`, deliberadamente fora das dependências
padrão (extra `cv`) — só é importado quando `--grid` é pedido, seguindo a
mesma disciplina de import tardio dos providers de OCR (`ocr/registry.py`).
"""

from __future__ import annotations

import re

from PIL import Image, ImageFilter, ImageStat

from .preprocess import BoundingBox, ImageError, detect_card_bounds


class CvUnavailableError(ImageError):
    """OpenCV/numpy não instalados — a detecção de grade precisa do extra `cv`."""

    def __init__(self) -> None:
        super().__init__(
            "A detecção de grade de cartas precisa do OpenCV.",
            hint='Instale com: pip install -e ".[cv]"',
        )


def ensure_cv_available() -> None:
    """Falha cedo e uma única vez, antes de criar qualquer `ScanJob`.

    Chamado por `ScanService.scan()` quando `grid=True`, não por imagem dentro
    dos workers — assim uma dependência ausente dá um erro claro na hora, em
    vez de N erros idênticos espalhados pelo pool de processos.
    """
    try:
        import cv2  # noqa: F401
        import numpy  # noqa: F401
    except ImportError as exc:
        raise CvUnavailableError() from exc


#: Proporção real de uma carta padrão (59mm x 86mm) — filtro de aspecto com a
#: mesma tolerância folgada que `detect_card_bounds` usa para área, porque
#: perspectiva de foto e escaneamento nunca dão o retângulo exato.
_CARD_ASPECT = 59 / 86  # ~0.686
_ASPECT_TOLERANCE = 0.18

#: Área mínima/máxima de uma célula, relativa à foto inteira. Abaixo de 2% é
#: ruído/detalhe; acima de 45%, é uma carta sozinha preenchendo o quadro —
#: isso não é uma grade, cai no caminho de carta única de hoje.
_MIN_CELL_AREA_RATIO = 0.02
_MAX_CELL_AREA_RATIO = 0.45

#: área do contorno / área do retângulo que o envolve. Perto de 1.0 = retângulo
#: de verdade; contornos irregulares (ruído, sombra) ficam bem abaixo disso.
_MIN_RECTANGULARITY = 0.75

#: Contornos cujo bounding rect se sobrepõe (IoU) acima disso são o mesmo
#: retângulo visto pela borda interna e externa — cv2 sempre acha as duas.
_DEDUPE_IOU = 0.6


def detect_grid_cells(
    image: Image.Image, *, min_cells: int = 2, max_cells: int = 16
) -> list[BoundingBox]:
    """Acha retângulos plausíveis de carta numa foto com várias cartas.

    Devolve `[]` sempre que não há confiança de que existe uma grade — nunca
    lança por causa de uma foto ruim, só por dependência ausente
    (`CvUnavailableError`, ver `ensure_cv_available`).

    As caixas devolvidas vêm em ordem de leitura (linha a linha, esquerda→
    direita), cobrindo grades NxM regulares ou irregulares.
    """
    ensure_cv_available()
    import cv2
    import numpy as np

    array = np.array(image.convert("L"))
    height, width = array.shape
    if width < 100 or height < 100:
        return []

    blurred = cv2.GaussianBlur(array, (5, 5), 0)
    thresh = cv2.adaptiveThreshold(
        blurred,
        255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY_INV,
        35,
        5,
    )
    contours, _ = cv2.findContours(thresh, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)

    frame_area = float(width * height)
    candidates: list[tuple[int, int, int, int]] = []
    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        if w <= 0 or h <= 0:
            continue
        area_ratio = (w * h) / frame_area
        if not (_MIN_CELL_AREA_RATIO <= area_ratio <= _MAX_CELL_AREA_RATIO):
            continue
        aspect = min(w, h) / max(w, h)
        if abs(aspect - _CARD_ASPECT) > _ASPECT_TOLERANCE:
            continue
        rectangularity = cv2.contourArea(contour) / (w * h)
        if rectangularity < _MIN_RECTANGULARITY:
            continue
        candidates.append((x, y, w, h))

    candidates = _dedupe_overlapping(candidates)
    if not (min_cells <= len(candidates) <= max_cells):
        return []

    ordered = _reading_order(candidates)
    return [
        BoundingBox(x / width, y / height, (x + w) / width, (y + h) / height)
        for x, y, w, h in ordered
    ]


class InvalidGridSizeError(ImageError):
    """`--grid-size`/`grid_size` num formato que não é `NxM` (N, M >= 1)."""

    def __init__(self, value: str) -> None:
        super().__init__(
            f"Tamanho de grade inválido: {value!r}.",
            hint="Use o formato LINHASxCOLUNAS, ex.: 3x3 ou 2x4.",
        )


def parse_grid_size(value: str) -> tuple[int, int]:
    """Converte `"3x3"` (espaços e maiúsculas tolerados) em `(rows, cols)`.

    Usado tanto pela CLI (`--grid-size`) quanto pela Web (`grid_size` no
    corpo de `POST /api/v1/scans`) — uma validação só, não uma por camada.
    """
    match = re.fullmatch(r"\s*(\d+)\s*[xX]\s*(\d+)\s*", value)
    if match is None:
        raise InvalidGridSizeError(value)
    rows, cols = int(match.group(1)), int(match.group(2))
    if rows < 1 or cols < 1:
        raise InvalidGridSizeError(value)
    return rows, cols


def manual_grid_cells(image: Image.Image, rows: int, cols: int) -> list[BoundingBox]:
    """Divide a foto em `rows` x `cols` células iguais, em ordem de leitura.

    Detecção automática por contorno (`detect_grid_cells`) funciona bem em
    cartas sintéticas de teste (bordas limpas, fundo liso), mas falha em
    fotos reais — a arte de verdade das cartas (textura, cor, sombra)
    confunde a filtragem por contorno (achado real: 2 de 9 cartas detectadas
    numa digitalização real de uma grade 3x3). Este é o caminho alternativo:
    o usuário informa o layout que já sabe que existe, sem precisar de
    OpenCV — só aritmética, mais a mesma detecção de borda Pillow-only que
    `detect_card_bounds` já usa para recortar a carta única, reaproveitada
    aqui para recortar a margem (mesa do scanner) ao redor do conjunto
    inteiro antes de dividir. Sem isso, sobra de fundo desloca a divisão e
    cada célula pega pedaço da carta vizinha (confirmado contra uma
    digitalização real com margem visível ao redor da grade).
    """
    bounds = detect_card_bounds(image)
    left, top, right, bottom = bounds if bounds is not None else (0, 0, *image.size)
    width, height = image.size
    cell_w = (right - left) / cols
    cell_h = (bottom - top) / rows
    return [
        BoundingBox(
            (left + col * cell_w) / width,
            (top + row * cell_h) / height,
            (left + (col + 1) * cell_w) / width,
            (top + (row + 1) * cell_h) / height,
        )
        for row in range(rows)
        for col in range(cols)
    ]


#: Limiares conservadores — o viés é sempre a favor de "não é vazio": um
#: "manter" falso só custa um OCR gasto à toa (como hoje), um "descartar"
#: falso perderia uma carta real.
_BLANK_STD_DEV_MAX = 8.0
_BLANK_EDGE_MEAN_MAX = 4.0


def is_cell_blank(
    image: Image.Image,
    box: BoundingBox,
    *,
    std_dev_max: float = _BLANK_STD_DEV_MAX,
    edge_mean_max: float = _BLANK_EDGE_MEAN_MAX,
) -> bool:
    """Uma célula de grade sem carta nenhuma — mesa do scanner vazia.

    Só se aplica ao layout manual (`--grid-size`): o usuário informa o
    layout que sabe existir, mas a página física pode ter menos cartas que
    células (ex.: 7 cartas numa disposição de grade 3x3). Uma célula vazia é
    lisa (baixo desvio-padrão de cinza) e sem bordas (baixa densidade de
    borda via `ImageFilter.FIND_EDGES`) — uma carta de verdade, mesmo mal
    iluminada, tem texto e arte que produzem as duas coisas.
    """
    width, height = image.size
    left = int(box.left * width)
    top = int(box.top * height)
    right = int(box.right * width)
    bottom = int(box.bottom * height)
    if right <= left or bottom <= top:
        return False

    cell = image.crop((left, top, right, bottom)).convert("L")
    std_dev = ImageStat.Stat(cell).stddev[0]
    if std_dev > std_dev_max:
        return False
    edge_mean = ImageStat.Stat(cell.filter(ImageFilter.FIND_EDGES)).mean[0]
    return edge_mean <= edge_mean_max


def _iou(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ix1, iy1 = max(ax, bx), max(ay, by)
    ix2, iy2 = min(ax + aw, bx + bw), min(ay + ah, by + bh)
    if ix2 <= ix1 or iy2 <= iy1:
        return 0.0
    intersection = (ix2 - ix1) * (iy2 - iy1)
    union = aw * ah + bw * bh - intersection
    return intersection / union if union > 0 else 0.0


def _dedupe_overlapping(
    boxes: list[tuple[int, int, int, int]],
) -> list[tuple[int, int, int, int]]:
    """Mantém só o maior de cada cluster de caixas sobrepostas.

    `cv2.findContours` sempre acha o contorno da borda interna e externa de
    uma mesma carta — sem isso, cada carta física viraria dois candidatos.
    """
    by_area = sorted(boxes, key=lambda box: box[2] * box[3], reverse=True)
    kept: list[tuple[int, int, int, int]] = []
    for box in by_area:
        if any(_iou(box, other) >= _DEDUPE_IOU for other in kept):
            continue
        kept.append(box)
    return kept


def _reading_order(
    boxes: list[tuple[int, int, int, int]],
) -> list[tuple[int, int, int, int]]:
    """Agrupa por linha (centro-Y próximo) e ordena esquerda→direita dentro
    de cada linha — cobre grades NxM regulares ou com espaçamento irregular.
    """
    if not boxes:
        return []

    avg_height = sum(h for _, _, _, h in boxes) / len(boxes)
    row_tolerance = avg_height * 0.5

    by_y = sorted(boxes, key=lambda box: box[1])
    rows: list[list[tuple[int, int, int, int]]] = []
    for box in by_y:
        _, y, _, h = box
        center_y = y + h / 2
        placed = False
        for row in rows:
            row_center_y = sum(b[1] + b[3] / 2 for b in row) / len(row)
            if abs(center_y - row_center_y) <= row_tolerance:
                row.append(box)
                placed = True
                break
        if not placed:
            rows.append([box])

    rows.sort(key=lambda row: sum(b[1] for b in row) / len(row))
    ordered: list[tuple[int, int, int, int]] = []
    for row in rows:
        ordered.extend(sorted(row, key=lambda box: box[0]))
    return ordered
