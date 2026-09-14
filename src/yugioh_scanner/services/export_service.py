"""Caso de uso de exportação (plano §15 e Fase 7).

Fica entre `repositories/collection.py` (dados do ORM) e `exporters/*`
(formatação pura): monta `CollectionRow`s achatadas e decide onde e como
escrever o arquivo — cada perfil só sabe formatar texto, nunca abre arquivo
nem conhece SQLAlchemy.
"""

from __future__ import annotations

from dataclasses import dataclass
from io import StringIO
from pathlib import Path

from sqlalchemy.orm import Session

from ..db.tables import CollectionItem
from ..exporters.base import CollectionRow, ExportProfile
from ..exporters.registry import create_profile
from ..repositories.collection import CollectionRepository


def _row_from_item(item: CollectionItem) -> CollectionRow:
    card_print = item.card_print
    return CollectionRow(
        item_id=item.id,
        card_id=item.card_id,
        card_name=item.card.name,
        quantity=item.quantity,
        condition=item.condition,
        edition=item.edition,
        language=item.language,
        notes=item.notes,
        source=item.source,
        added_at=item.added_at.isoformat(),
        card_print_id=item.card_print_id,
        set_code_full=item.set_code_full_display,
        set_prefix=card_print.set_prefix if card_print else None,
        region=card_print.region if card_print else None,
        set_name=card_print.set_name if card_print else None,
        rarity=item.rarity_display,
    )


@dataclass
class ExportReport:
    fmt: str
    profile: str
    rows_written: int
    rows_skipped: int
    path: Path | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "format": self.fmt,
            "profile": self.profile,
            "rows_written": self.rows_written,
            "rows_skipped": self.rows_skipped,
            "path": str(self.path) if self.path else None,
        }


class ExportService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.repo = CollectionRepository(session)

    def build_rows(self) -> list[CollectionRow]:
        """Todas as linhas, na mesma ordem de `collection list` (por nome).

        Sem filtro aqui de propósito: `--skip-unresolved` é decidido uma vez,
        em `_select_rows`, para nunca consultar o banco duas vezes só para
        contar quantas linhas foram omitidas.
        """
        items = self.repo.list_filtered(sort="name")
        return [_row_from_item(item) for item in items]

    def export_to_path(
        self,
        path: Path,
        *,
        fmt: str,
        profile_name: str | None = None,
        skip_unresolved: bool = False,
    ) -> ExportReport:
        """Exporta para um arquivo, com o encoding/newline exatos do perfil."""
        profile = create_profile(fmt, profile_name)
        rows, skipped = self._select_rows(skip_unresolved)

        path.parent.mkdir(parents=True, exist_ok=True)
        # `newline=""` é obrigatório com `csv.writer` (recomendação do próprio
        # módulo `csv`): sem isso, no Windows o `\r\n` do writer viraria `\r\r\n`.
        with path.open("w", encoding=profile.encoding, newline="") as handle:
            profile.render(rows, handle)

        return ExportReport(
            fmt=fmt,
            profile=profile.name,
            rows_written=len(rows),
            rows_skipped=skipped,
            path=path,
        )

    def export_to_string(
        self,
        *,
        fmt: str,
        profile_name: str | None = None,
        skip_unresolved: bool = False,
    ) -> tuple[str, ExportProfile]:
        """Exporta para uma string em memória — usado por `export --stdout`
        e pelos testes (golden files sem tocar em disco)."""
        profile = create_profile(fmt, profile_name)
        rows, _skipped = self._select_rows(skip_unresolved)
        buffer = StringIO(newline="")
        profile.render(rows, buffer)
        return buffer.getvalue(), profile

    def _select_rows(self, skip_unresolved: bool) -> tuple[list[CollectionRow], int]:
        all_rows = self.build_rows()
        if not skip_unresolved:
            return all_rows, 0
        selected = [row for row in all_rows if row.has_print]
        return selected, len(all_rows) - len(selected)
