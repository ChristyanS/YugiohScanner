"""Preparação da imagem antes do OCR (plano §5.2).

A ordem das etapas importa e cada uma resolve um problema concreto:

1. **Validação estrutural** — arquivo corrompido é rejeitado antes de gastar CPU.
2. **Guarda de decompression bomb** — um PNG de 40 KB pode virar 20 GB em RAM.
3. **Orientação EXIF** — foto de celular chega girada; sem corrigir, o OCR erra
   100% das vezes.
4. **Downscale** — 12 MP não lê melhor que 1600 px, e custa 5× mais tempo.
5. **Recorte da carta** — tira o fundo da foto, quando dá para achar a borda.
6. **ROIs** — o layout da carta é fixo: nome em cima, código embaixo à direita.
   Rodar OCR só nessas duas faixas é o maior ganho isolado do pipeline.

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

#: Faixa do set code: canto inferior direito, acima do texto de copyright.
CODE_ROI = BoundingBox(0.52, 0.855, 0.99, 0.935)


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


def prepare_image(
    path: Path,
    *,
    max_pixels: int,
    auto_crop: bool = True,
    max_dimension: int = MAX_DIMENSION,
) -> PreparedImage:
    """Pipeline completo: arquivo → regiões prontas para o OCR."""
    image = load_image(path, max_pixels=max_pixels)
    notes: dict[str, Any] = {"original_size": list(image.size)}

    try:
        if auto_crop:
            bounds = detect_card_bounds(image)
            if bounds is not None:
                image = image.crop(bounds)
                notes["cropped_to"] = list(bounds)
            else:
                notes["cropped_to"] = None

        image = downscale(image, max_dimension)
        notes["working_size"] = list(image.size)

        width, height = image.size
        name_crop = image.crop(NAME_ROI.to_pixels(width, height))
        code_crop = image.crop(CODE_ROI.to_pixels(width, height))

        regions = {
            "name": enhance_for_ocr(name_crop),
            # 2× porque o código é o texto menor da carta.
            "code": enhance_for_ocr(code_crop, upscale=2),
            # A imagem inteira fica disponível como rede de segurança: se as
            # ROIs não renderem texto, o pipeline tenta nela.
            "full": enhance_for_ocr(image),
        }
        name_crop.close()
        code_crop.close()
        return PreparedImage(source=path, width=width, height=height, regions=regions, notes=notes)
    finally:
        image.close()
