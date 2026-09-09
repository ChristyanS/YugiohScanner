"""Casamento entre o texto lido e o catálogo local (plano §7)."""

from .candidates import CandidateFinder, NameCandidate, NameIndex
from .engine import MatchingEngine, MatchResult
from .resolver import CodeResolution, PrintMatch, PrintResolver

__all__ = [
    "CandidateFinder",
    "CodeResolution",
    "MatchResult",
    "MatchingEngine",
    "NameCandidate",
    "NameIndex",
    "PrintMatch",
    "PrintResolver",
]
