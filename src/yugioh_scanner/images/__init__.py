"""Preparação de imagens e cache local das artes do catálogo."""

from .cache import ImageCache, ImageNotAvailableError, ImageSize
from .preprocess import (
    CODE_ROI,
    MAX_DIMENSION,
    NAME_ROI,
    SUPPORTED_EXTENSIONS,
    BoundingBox,
    ImageError,
    PreparedImage,
    detect_card_bounds,
    downscale,
    enhance_for_ocr,
    has_supported_extension,
    load_image,
    looks_like_image,
    prepare_image,
)

__all__ = [
    "CODE_ROI",
    "MAX_DIMENSION",
    "NAME_ROI",
    "SUPPORTED_EXTENSIONS",
    "BoundingBox",
    "ImageCache",
    "ImageError",
    "ImageNotAvailableError",
    "ImageSize",
    "PreparedImage",
    "detect_card_bounds",
    "downscale",
    "enhance_for_ocr",
    "has_supported_extension",
    "load_image",
    "looks_like_image",
    "prepare_image",
]
