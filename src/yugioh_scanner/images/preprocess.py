"""Preparação da imagem antes do OCR (plano §5.2).

A ordem das etapas importa e cada uma resolve um problema concreto:

1. **Validação estrutural** — arquivo corrompido é rejeitado antes de gastar CPU.
2. **Guarda de decompression bomb** — um PNG de 40 KB pode virar 20 GB em RAM.
3. **Orientação EXIF** — foto de celular chega girada; sem corrigir, o OCR erra
   100% das vezes.
4. **Downscale** — 12 MP não lê melhor que 1600 px, e custa 5× mais tempo.
5. **Recorte da carta** — tira o fundo da foto, quando dá para achar a borda.
6. **ROIs** — o layout da carta é fixo: nome em cima, código embaixo à
   direita, passcode ("Card ID") embaixo à esquerda. Rodar OCR só nessas
   faixas é o maior ganho isolado do pipeline.

Limitação assumida: as ROIs são proporcionais e pressupõem a foto enquadrada na
carta. A detecção de borda da §5 mitiga isso; quando ela não tem confiança,
caímos para a imagem inteira em vez de recortar errado.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PIL import Image, ImageFilter, ImageOps, ImageStat

from ..errors import YugiohScannerError

#: Extensões aceitas (plano §5 do briefing).
SUPPORTED_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png"})

#: Maior dimensão depois do downscale.
MAX_DIMENSION = 1600

#: Lado da miniatura usada na detecção de borda, e quantos pixels das bordas
#: dela são descartados (o filtro de bordas sempre acende a moldura).
_PROBE = 200
_PAD = 3

#: Assinaturas de arquivo. A extensão mente; os primeiros bytes, não.
_MAGIC_BYTES: tuple[bytes, ...] = (
    b"\xff\xd8\xff",  # JPEG
    b"\x89PNG\r\n\x1a\n",  # PNG
)


@dataclass(frozen=True, slots=True)
class BoundingBox:
    """Retângulo em coordenadas relativas (0..1), independente da resolução."""

    left: float
    top: float
    right: float
    bottom: float

    def to_pixels(self, width: int, height: int) -> tuple[int, int, int, int]:
        return (
            max(0, int(self.left * width)),
            max(0, int(self.top * height)),
            min(width, int(self.right * width)),
            min(height, int(self.bottom * height)),
        )


#: Faixa do nome: topo da carta, deixando de fora o ícone de atributo à direita.
NAME_ROI = BoundingBox(0.04, 0.025, 0.82, 0.115)

#: Faixa do set code — a linha impressa logo abaixo da moldura da arte,
#: rente à borda superior da caixa de texto/efeito (ex.: "RA05-PT058").
#: **Bug real corrigido** (achado do usuário): a versão anterior
#: (`0.69`–`0.775`) media uma faixa alta demais — capturava o próprio código
#: *junto* com a linha de tipo ("[MÁQUINA/LINK/EFEITO]") logo abaixo dela, e
#: o OCR ora lia uma, ora outra, ora as duas misturadas.
#:
#: Calibrado em duas rodadas contra fotos reais: primeiro 2 fotos
#: (`examples/2026091213*.jpg`, código entre ~70% e ~72,3% da altura), depois
#: as 10 fotos de `imagens-calibration-cellphone/` (nome de arquivo
#: `{passcode}-{setcode}.jpg` — o gabarito). A faixa estreita calibrada só
#: pelas 2 primeiras (0.693-0.735) já falhava em metade das 10 — não por o
#: código estar em outro lugar, mas porque `detect_card_bounds` não recorta
#: com a mesma folga em toda foto (a mesma margem física gera uma
#: porcentagem de altura ligeiramente diferente conforme o quanto a borda
#: detectada "sobra" em cada foto). Alargada para 0.665-0.76 — folga que
#: cobre a variação real medida sem devolver a invadir a linha de tipo (10/10
#: fotos calibradas leem o código limpo depois disso). `tests/factories.py`
#: desenha o set code sintético na mesma posição — plano de idiomas,
#: continuação: um ROI corrigido só aqui, sem mexer no gerador de teste,
#: deixaria a suíte "passando" contra um layout que carta nenhuma tem.
CODE_ROI = BoundingBox(0.42, 0.665, 0.98, 0.76)

#: Variante de `CODE_ROI` para o caminho de grade, quando a célula já passou
#: por `images/grid.py::locate_and_deskew_card` antes de chegar aqui — achado
#: real desta sessão (`tests/fixtures/grid_scans/`, medido em 5 cartas): o
#: recorte endireitado tem margem quase zero (ao contrário do
#: `detect_card_bounds` de cima, mais folgado), então a mesma fração de altura
#: cai em lugar diferente. A arte termina entre 71%-74% da altura — bem abaixo
#: do topo de 0.665 acima, que por isso capturava um bocado de arte de alto
#: contraste como ruído antes do código de verdade. O código fica entre
#: ~73%-76%, colado na borda da caixa de tipo ("[GUERREIRO]" etc., que só
#: começa a partir de ~77%-79%) — por isso o topo sobe (corta a arte) e a base
#: sobe só o suficiente para caber o código inteiro sem invadir o texto de
#: tipo.
CODE_ROI_GRID = BoundingBox(0.40, 0.70, 0.98, 0.775)

#: Faixa do passcode ("Card ID") — canto inferior-esquerdo, rente à borda
#: física da carta (mesma linha do copyright "©2020 Studio Dice/...", bem
#: abaixo da caixa de texto/efeito — **não** a mesma altura do set code,
#: como a estimativa inicial por mirror horizontal assumia).
#:
#: Mesma história do `CODE_ROI`: as 2 fotos iniciais mediram ~95,5%-97,5%,
#: mas a faixa estreita perdia o passcode em várias das 10 fotos de
#: calibração (o texto ficava abaixo do limite inferior — de novo, variação
#: de `detect_card_bounds`, não de posição real). Alargada para 0.90-1.0 —
#: como não há nada de interesse abaixo do passcode/copyright, dá pra ser
#: generoso sem risco de invadir outra coisa. Efeito colateral aceito: com a
#: faixa mais alta, a linha "1ª Edição" abaixo do passcode às vezes entra
#: junto e o OCR funde as duas numa caixa só — por isso
#: `domain/passcode.py::clean_passcode` extrai o maior run de dígitos da
#: leitura em vez de exigir que ela seja só dígitos.
PASSCODE_ROI = BoundingBox(0.00, 0.90, 0.34, 1.0)


class ImageError(YugiohScannerError):
    """A imagem não pôde ser lida ou é inaceitável.

    Erro *por imagem*: o pipeline registra e segue para a próxima (plano §16).
    """


@dataclass
class PreparedImage:
    """Resultado do pré-processamento, pronto para o OCR."""

    source: Path
    width: int
    height: int
    regions: dict[str, Image.Image] = field(default_factory=dict)
    #: Como as ROIs foram obtidas — vira log e ajuda a diagnosticar erro de
    #: enquadramento sem abrir a foto.
    notes: dict[str, Any] = field(default_factory=dict)

    def close(self) -> None:
        for image in self.regions.values():
            image.close()


def has_supported_extension(path: Path) -> bool:
    return path.suffix.lower() in SUPPORTED_EXTENSIONS


def looks_like_image(path: Path) -> bool:
    """Confere os bytes mágicos.

    A extensão é entrada do usuário; um `.jpg` pode ser qualquer coisa. Esta
    checagem é barata e evita entregar lixo ao decodificador (plano §21).
    """
    try:
        with path.open("rb") as handle:
            header = handle.read(8)
    except OSError:
        return False
    return any(header.startswith(magic) for magic in _MAGIC_BYTES)


def load_image(path: Path, *, max_pixels: int) -> Image.Image:
    """Abre, valida e normaliza a orientação de uma imagem.

    Levanta `ImageError` — nunca deixa vazar exceção de biblioteca, porque quem
    chama precisa tratar isso como dado, não como falha do programa.
    """
    if not path.is_file():
        raise ImageError(f"Arquivo não encontrado: {path.name}")
    if not has_supported_extension(path):
        raise ImageError(f"Extensão não suportada: {path.name}")
    if not looks_like_image(path):
        raise ImageError(f"O conteúdo de {path.name} não é um JPEG/PNG válido.")

    try:
        # `verify()` detecta corrupção estrutural, mas invalida o handle:
        # é obrigatório reabrir depois.
        with Image.open(path) as probe:
            probe.verify()
        opened = Image.open(path)
        width, height = opened.size
        if width * height > max_pixels:
            opened.close()
            raise ImageError(
                f"{path.name} tem {width}x{height} px, acima do limite de {max_pixels:,} px.",
                hint="Ajuste YGS_MAX_IMAGE_PIXELS se a imagem for legítima.",
            )
        # Aplica a rotação do EXIF e descarta o metadado.
        rotated = ImageOps.exif_transpose(opened)
        return rotated.convert("RGB")
    except ImageError:
        raise
    except (OSError, SyntaxError, ValueError) as exc:
        raise ImageError(f"Não foi possível ler {path.name}: {exc}") from exc


def downscale(image: Image.Image, max_dimension: int = MAX_DIMENSION) -> Image.Image:
    """Reduz mantendo a proporção. Devolve a mesma imagem se já couber."""
    width, height = image.size
    longest = max(width, height)
    if longest <= max_dimension:
        return image
    scale = max_dimension / longest
    return image.resize((int(width * scale), int(height * scale)), Image.Resampling.LANCZOS)


def detect_card_bounds(image: Image.Image) -> tuple[int, int, int, int] | None:
    """Tenta achar a borda da carta dentro da foto.

    Heurística deliberadamente simples: detecta bordas em uma miniatura e pega
    a caixa que as contém. Funciona bem com a carta sobre fundo liso, que é o
    caso de uso alvo.

    Devolve `None` quando o resultado não é confiável — recortar errado é pior
    do que não recortar, porque desloca as ROIs e estraga as duas leituras.
    """
    width, height = image.size
    if width < 50 or height < 50:
        return None

    thumbnail = image.convert("L").resize((_PROBE, _PROBE), Image.Resampling.BILINEAR)
    edges = thumbnail.filter(ImageFilter.FIND_EDGES)
    # O FIND_EDGES sempre acende a moldura da própria miniatura; sem descartar
    # essas bordas, a caixa resultante é a imagem inteira, sempre.
    edges = edges.crop((_PAD, _PAD, _PROBE - _PAD, _PROBE - _PAD))

    # Limiar relativo ao próprio conteúdo: fotos escuras e claras têm faixas de
    # gradiente bem diferentes.
    threshold = max(20, int(ImageStat.Stat(edges).mean[0] * 2.5))
    mask = edges.point(lambda pixel: 255 if pixel > threshold else 0)
    box = mask.getbbox()
    if box is None:
        return None

    left, top, right, bottom = (value + _PAD for value in box)
    area_ratio = ((right - left) * (bottom - top)) / (_PROBE * _PROBE)
    # Muito pequeno = achou um detalhe, não a carta.
    # Muito grande = o fundo tem textura e a caixa virou a imagem inteira.
    if not 0.25 <= area_ratio <= 0.97:
        return None

    scale_x, scale_y = width / _PROBE, height / _PROBE
    return (
        int(left * scale_x),
        int(top * scale_y),
        int(right * scale_x),
        int(bottom * scale_y),
    )


def enhance_for_ocr(image: Image.Image, *, upscale: int = 1) -> Image.Image:
    """Grayscale + autocontraste, com upscale opcional.

    O upscale existe para o set code: ele é impresso em corpo minúsculo e, sem
    ampliar, a taxa de acerto despenca.
    """
    result = ImageOps.autocontrast(image.convert("L"), cutoff=1)
    if upscale > 1:
        result = result.resize(
            (result.width * upscale, result.height * upscale), Image.Resampling.LANCZOS
        )
        result = result.filter(ImageFilter.SHARPEN)
    return result


def prepare_regions(
    image: Image.Image,
    *,
    source: Path,
    region: BoundingBox | None = None,
    auto_crop: bool = True,
    max_dimension: int = MAX_DIMENSION,
    code_roi: BoundingBox = CODE_ROI,
) -> PreparedImage:
    """Pipeline completo a partir de uma imagem já carregada.

    `region`, quando informado, recorta primeiro para aquele retângulo (uma
    célula de uma grade de cartas, plano §22) antes de rodar exatamente o
    mesmo refino de sempre (`detect_card_bounds` + ROIs) sobre o recorte. Não
    fecha `image`: quem chama pode ser dono dela (ex.: uma foto com várias
    células, preparada uma vez e recortada N vezes) — só os intermediários
    criados aqui são fechados.

    `code_roi` é injetável porque a proporção certa depende de como `image`
    já chegou: uma célula de grade endireitada por
    `images/grid.py::locate_and_deskew_card` tem margem quase zero, diferente
    da foto de carta única — ver `CODE_ROI_GRID`. O padrão (`CODE_ROI`)
    mantém o comportamento de sempre para quem não passa nada.
    """
    notes: dict[str, Any] = {"original_size": list(image.size)}
    to_close: list[Image.Image] = []

    working = image
    if region is not None:
        working = image.crop(region.to_pixels(*image.size))
        to_close.append(working)
        notes["grid_region"] = [region.left, region.top, region.right, region.bottom]

    try:
        if auto_crop:
            bounds = detect_card_bounds(working)
            if bounds is not None:
                working = working.crop(bounds)
                to_close.append(working)
                notes["cropped_to"] = list(bounds)
            else:
                notes["cropped_to"] = None

        working = downscale(working, max_dimension)
        to_close.append(working)
        notes["working_size"] = list(working.size)

        width, height = working.size
        name_crop = working.crop(NAME_ROI.to_pixels(width, height))
        code_crop = working.crop(code_roi.to_pixels(width, height))
        passcode_crop = working.crop(PASSCODE_ROI.to_pixels(width, height))

        regions = {
            "name": enhance_for_ocr(name_crop),
            # 2× porque o código é o texto menor da carta.
            "code": enhance_for_ocr(code_crop, upscale=2),
            # Mesmo tratamento do código: texto igualmente pequeno.
            "passcode": enhance_for_ocr(passcode_crop, upscale=2),
            # A imagem inteira fica disponível como rede de segurança: se as
            # ROIs não renderem texto, o pipeline tenta nela.
            "full": enhance_for_ocr(working),
        }
        name_crop.close()
        code_crop.close()
        passcode_crop.close()
        return PreparedImage(
            source=source, width=width, height=height, regions=regions, notes=notes
        )
    finally:
        closed: set[int] = set()
        for candidate in to_close:
            if candidate is image or id(candidate) in closed:
                continue
            closed.add(id(candidate))
            candidate.close()


def prepare_image(
    path: Path,
    *,
    max_pixels: int,
    auto_crop: bool = True,
    max_dimension: int = MAX_DIMENSION,
) -> PreparedImage:
    """Pipeline completo: arquivo → regiões prontas para o OCR."""
    image = load_image(path, max_pixels=max_pixels)
    try:
        return prepare_regions(
            image, source=path, region=None, auto_crop=auto_crop, max_dimension=max_dimension
        )
    finally:
        image.close()
