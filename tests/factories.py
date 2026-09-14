"""Geradores de dados para testes.

As "cartas" sintéticas imitam o **layout** de uma carta de Yu-Gi-Oh! (nome na
faixa superior, set code rente à borda da caixa de texto/efeito, passcode no
canto inferior-esquerdo) na proporção real 59×86 mm. Isso permite exercitar
ROI, pré-processamento e até o OCR de verdade sem depender de um corpus de
fotos — que, conforme o plano §19.4, entra na Fase 9.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from yugioh_scanner.images.preprocess import BoundingBox

#: Proporção real de uma carta (59 mm × 86 mm).
CARD_ASPECT = 59 / 86


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Fonte com tamanho controlado, caindo para a embutida se preciso."""
    for candidate in ("arial.ttf", "DejaVuSans.ttf", "LiberationSans-Regular.ttf"):
        try:
            return ImageFont.truetype(candidate, size)
        except OSError:
            continue
    return ImageFont.load_default()


def make_card_image(
    path: Path,
    *,
    name: str = "BLUE-EYES WHITE DRAGON",
    set_code: str = "LOB-001",
    passcode: str = "12345678",
    height: int = 860,
    background: tuple[int, int, int] = (196, 168, 96),
    margin: int = 0,
    margin_color: tuple[int, int, int] = (30, 30, 30),
) -> Path:
    """Gera uma carta sintética com nome e set code nas posições reais.

    `margin` simula a foto com fundo em volta da carta, que é o caso que a
    detecção de borda precisa resolver.
    """
    width = int(height * CARD_ASPECT)
    card = Image.new("RGB", (width, height), background)
    draw = ImageDraw.Draw(card)

    # Moldura, para a detecção de borda ter o que encontrar.
    draw.rectangle([(4, 4), (width - 5, height - 5)], outline=(120, 100, 50), width=3)

    # Nome: faixa superior (~2,5% a 11,5% da altura — mesma ROI do preprocess).
    draw.rectangle(
        [(int(width * 0.04), int(height * 0.02)), (int(width * 0.90), int(height * 0.12))],
        fill=(232, 214, 160),
    )
    # Cartas reais comprimem a fonte para o nome caber na faixa; sem isso, o
    # nome sintético transborda a ROI e o teste mediria um artefato nosso.
    name_box_width = int(width * 0.90) - int(width * 0.06)
    font_size = max(14, int(height * 0.045))
    name_font = _font(font_size)
    while font_size > 10 and draw.textlength(name, font=name_font) > name_box_width:
        font_size -= 1
        name_font = _font(font_size)
    draw.text((int(width * 0.06), int(height * 0.035)), name, fill=(20, 20, 20), font=name_font)

    # Arte, só para a imagem não ser uma chapa lisa.
    draw.rectangle(
        [(int(width * 0.12), int(height * 0.16)), (int(width * 0.88), int(height * 0.665))],
        fill=(90, 120, 170),
        outline=(40, 40, 40),
    )

    # Set code: rente à borda superior da caixa de texto (mesma ROI real do
    # preprocess — corrigida contra fotos reais, ver `CODE_ROI`). A posição
    # antiga aqui (0.705) coincidia com o bug real da ROI (0.69-0.775, alta
    # demais); mantê-la desatualizada depois de corrigir só o `preprocess.py`
    # teria deixado os testes "passando" contra um layout que nenhuma carta
    # de verdade tem.
    draw.text(
        (int(width * 0.50), int(height * 0.71)),
        set_code,
        fill=(15, 15, 15),
        font=_font(max(11, int(height * 0.032))),
    )

    # Caixa de texto da carta.
    draw.rectangle(
        [(int(width * 0.07), int(height * 0.80)), (int(width * 0.93), int(height * 0.93))],
        fill=(226, 214, 186),
    )

    # Passcode ("Card ID"): canto inferior-esquerdo, rente à borda física da
    # carta — mesma ROI real do preprocess (`PASSCODE_ROI`, corrigida contra
    # fotos reais; a estimativa inicial por mirror do `CODE_ROI` media a
    # mesma altura errada dele).
    draw.text(
        (int(width * 0.02), int(height * 0.955)),
        passcode,
        fill=(15, 15, 15),
        font=_font(max(10, int(height * 0.025))),
    )

    if margin:
        framed = Image.new("RGB", (width + margin * 2, height + margin * 2), margin_color)
        framed.paste(card, (margin, margin))
        card.close()
        card = framed

    path.parent.mkdir(parents=True, exist_ok=True)
    card.save(path, quality=95)
    card.close()
    return path


def make_corrupted_image(path: Path) -> Path:
    """Arquivo com extensão de imagem e conteúdo inválido."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"isto definitivamente nao e um jpeg")
    return path


def make_truncated_jpeg(path: Path) -> Path:
    """JPEG com cabeçalho válido e corpo cortado — corrupção real."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # O nome precisa terminar em .jpg: o Pillow infere o formato pela extensão.
    buffer = path.with_name(f"_tmp_{path.stem}.jpg")
    make_card_image(buffer, height=200)
    data = buffer.read_bytes()
    buffer.unlink()
    path.write_bytes(data[: len(data) // 3])
    return path


def make_grid_photo(
    folder: Path, filename: str, readings: list[dict[str, str]]
) -> tuple[Path, list[BoundingBox]]:
    """Compõe N cartas sintéticas numa única foto, em duas colunas (plano §22).

    A foto não precisa "parecer" uma grade de verdade para quem testa a
    detecção de contornos de verdade: os testes de pipeline (grid=True)
    fakeiam `detect_grid_cells` para devolver as caixas conhecidas por esta
    função, em vez de detectar de verdade — a suíte rápida não pode depender
    do extra `cv` estar instalado (ADR 0011). Devolve o caminho da foto e as
    caixas normalizadas (0..1) de cada célula, na ordem de `readings`.
    """
    folder.mkdir(parents=True, exist_ok=True)
    tiles = []
    for i, reading in enumerate(readings):
        tile_path = folder / f"_tile_{i}.jpg"
        make_card_image(
            tile_path,
            name=reading.get("name", "X"),
            set_code=reading.get("code") or "LOB-001",
            height=300,
        )
        tiles.append(Image.open(tile_path))

    cols = 2
    cell_w = max(tile.width for tile in tiles)
    cell_h = max(tile.height for tile in tiles)
    gutter = 20
    rows = (len(tiles) + cols - 1) // cols
    canvas_w = cols * cell_w + (cols + 1) * gutter
    canvas_h = rows * cell_h + (rows + 1) * gutter
    canvas = Image.new("RGB", (canvas_w, canvas_h), (10, 10, 10))

    boxes: list[BoundingBox] = []
    for i, tile in enumerate(tiles):
        row, col = divmod(i, cols)
        x = gutter + col * (cell_w + gutter)
        y = gutter + row * (cell_h + gutter)
        canvas.paste(tile, (x, y))
        boxes.append(
            BoundingBox(
                x / canvas_w,
                y / canvas_h,
                (x + tile.width) / canvas_w,
                (y + tile.height) / canvas_h,
            )
        )
        tile.close()

    dest = folder / filename
    canvas.save(dest, quality=95)
    canvas.close()
    for i in range(len(readings)):
        (folder / f"_tile_{i}.jpg").unlink()
    return dest, boxes


def make_folder_with_cards(folder: Path, cards: dict[str, tuple[str, str]]) -> Path:
    """Cria uma pasta de scan. `cards` mapeia arquivo → (nome, set code)."""
    folder.mkdir(parents=True, exist_ok=True)
    for filename, (name, code) in cards.items():
        make_card_image(folder / filename, name=name, set_code=code)
    return folder
