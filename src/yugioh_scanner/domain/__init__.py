"""Regras puras do domínio: sem I/O, sem SQLAlchemy, sem HTTP."""

from .confidence import (
    ConfidenceInput,
    ConfidenceThresholds,
    Decision,
    compute_confidence,
    decide,
)
from .normalization import normalize_for_display, normalize_fuzzy, normalize_strict
from .setcode import (
    SetCode,
    correction_variants,
    extract_prefix,
    looks_like_set_code,
    normalize_set_code,
    parse_set_code,
)

__all__ = [
    "ConfidenceInput",
    "ConfidenceThresholds",
    "Decision",
    "SetCode",
    "compute_confidence",
    "correction_variants",
    "decide",
    "extract_prefix",
    "looks_like_set_code",
    "normalize_for_display",
    "normalize_fuzzy",
    "normalize_set_code",
    "normalize_strict",
    "parse_set_code",
]
