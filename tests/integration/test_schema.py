"""Constraints e índices do schema (plano §4.3).

Estes testes existem porque as regras mais importantes do modelo de dados são
justamente as que o SQLite *não* aplica sozinho: unicidade com NULL, CHECK de
vocabulário e chaves estrangeiras.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from yugioh_scanner.db.tables import (
    Card,
    CardAltName,
    CardPrint,
    CardSet,
    CollectionItem,
    ScanImage,
    ScanJob,
)


def make_card(
    session: Session, card_id: int = 89631139, name: str = "Blue-Eyes White Dragon"
) -> Card:
    card = Card(
        id=card_id,
        name=name,
        name_normalized=name.lower(),
        type="Normal Monster",
        desc="Lendário dragão.",
        atk=3000,
        defense=2500,
        level=8,
        attribute="LIGHT",
        race="Dragon",
    )
    session.add(card)
    session.flush()
    return card


def make_print(session: Session, card: Card, code: str, rarity: str | None) -> CardPrint:
    card_print = CardPrint(
        card_id=card.id,
        set_code_full=code,
        set_code_normalized=code.upper(),
        set_name="Legend of Blue Eyes White Dragon",
        rarity=rarity,
    )
    session.add(card_print)
    session.flush()
    return card_print


class TestPragmas:
    def test_foreign_keys_enforced(self, engine: Engine) -> None:
        """Sem `PRAGMA foreign_keys=ON`, todos os ondelete viram decoração."""
        with engine.connect() as conn:
            assert conn.execute(text("PRAGMA foreign_keys")).scalar() == 1

    def test_wal_mode(self, engine: Engine) -> None:
        with engine.connect() as conn:
            assert conn.execute(text("PRAGMA journal_mode")).scalar() == "wal"


class TestCardPrintUniqueness:
    def test_same_card_same_set_different_rarity_is_allowed(self, session: Session) -> None:
        """Caso real da API (§0.3.3) — a raridade faz parte da identidade."""
        card = make_card(session)
        make_print(session, card, "LOB-001", "Ultra Rare")
        make_print(session, card, "LOB-001", "Secret Rare")
        session.flush()
        assert session.query(CardPrint).count() == 2

    def test_exact_duplicate_print_is_rejected(self, session: Session) -> None:
        card = make_card(session)
        make_print(session, card, "LOB-001", "Ultra Rare")
        with pytest.raises(IntegrityError):
            make_print(session, card, "LOB-001", "Ultra Rare")

    def test_duplicate_with_null_rarity_is_rejected(self, session: Session) -> None:
        """O COALESCE(rarity,'') do índice cobre o caso de raridade ausente."""
        card = make_card(session)
        make_print(session, card, "LOB-001", None)
        with pytest.raises(IntegrityError):
            make_print(session, card, "LOB-001", None)


class TestCollectionUniqueness:
    def test_duplicate_item_with_print_is_rejected(self, session: Session) -> None:
        card = make_card(session)
        card_print = make_print(session, card, "LOB-001", "Ultra Rare")
        session.add(CollectionItem(card_id=card.id, card_print_id=card_print.id, quantity=1))
        session.flush()
        session.add(CollectionItem(card_id=card.id, card_print_id=card_print.id, quantity=1))
        with pytest.raises(IntegrityError):
            session.flush()

    def test_duplicate_item_without_print_is_rejected(self, session: Session) -> None:
        """O caso que o SQLite deixaria passar: NULL != NULL em UNIQUE.

        Sem o índice por expressão, "carta sem set definido" poderia ser
        inserida infinitas vezes e a coleção inflaria a cada scan.
        """
        card = make_card(session)
        session.add(CollectionItem(card_id=card.id, card_print_id=None, quantity=1))
        session.flush()
        session.add(CollectionItem(card_id=card.id, card_print_id=None, quantity=1))
        with pytest.raises(IntegrityError):
            session.flush()

    def test_same_card_with_and_without_print_coexist(self, session: Session) -> None:
        """Ter LOB-001 identificada e outra cópia sem set é legítimo."""
        card = make_card(session)
        card_print = make_print(session, card, "LOB-001", "Ultra Rare")
        session.add(CollectionItem(card_id=card.id, card_print_id=card_print.id, quantity=1))
        session.add(CollectionItem(card_id=card.id, card_print_id=None, quantity=1))
        session.flush()
        assert session.query(CollectionItem).count() == 2

    def test_different_condition_is_a_different_item(self, session: Session) -> None:
        card = make_card(session)
        card_print = make_print(session, card, "LOB-001", "Ultra Rare")
        session.add(
            CollectionItem(
                card_id=card.id, card_print_id=card_print.id, quantity=3, condition="Near Mint"
            )
        )
        session.add(
            CollectionItem(
                card_id=card.id, card_print_id=card_print.id, quantity=1, condition="Damaged"
            )
        )
        session.flush()
        assert session.query(CollectionItem).count() == 2


class TestCheckConstraints:
    def test_quantity_must_be_positive(self, session: Session) -> None:
        """Quantidade 0 significa 'não tenho' → a linha some, não fica zerada."""
        card = make_card(session)
        session.add(CollectionItem(card_id=card.id, quantity=0))
        with pytest.raises(IntegrityError):
            session.flush()

    def test_negative_quantity_rejected(self, session: Session) -> None:
        card = make_card(session)
        session.add(CollectionItem(card_id=card.id, quantity=-1))
        with pytest.raises(IntegrityError):
            session.flush()

    def test_condition_vocabulary_enforced(self, session: Session) -> None:
        """O vocabulário fechado é o que garante um CSV importável (§15.2)."""
        card = make_card(session)
        session.add(CollectionItem(card_id=card.id, quantity=1, condition="Meia-boca"))
        with pytest.raises(IntegrityError):
            session.flush()

    def test_edition_vocabulary_enforced(self, session: Session) -> None:
        card = make_card(session)
        session.add(CollectionItem(card_id=card.id, quantity=1, edition="Segunda"))
        with pytest.raises(IntegrityError):
            session.flush()

    def test_source_vocabulary_enforced(self, session: Session) -> None:
        card = make_card(session)
        session.add(CollectionItem(card_id=card.id, quantity=1, source="telepatia"))
        with pytest.raises(IntegrityError):
            session.flush()

    def test_scan_job_status_vocabulary_enforced(self, session: Session) -> None:
        session.add(ScanJob(folder_path="/x", ocr_provider="fake", status="talvez"))
        with pytest.raises(IntegrityError):
            session.flush()


class TestScanImageIdempotency:
    def test_file_hash_is_globally_unique(self, session: Session) -> None:
        """A mesma foto não pode ser processada duas vezes, nem em jobs
        diferentes nem sob outro nome de arquivo (plano §13.1)."""
        job_a = ScanJob(folder_path="/a", ocr_provider="fake")
        job_b = ScanJob(folder_path="/b", ocr_provider="fake")
        session.add_all([job_a, job_b])
        session.flush()

        session.add(ScanImage(job_id=job_a.id, file_path="/a/IMG_001.jpg", file_hash="abc123"))
        session.flush()

        # Outro job, outro caminho, mesmo conteúdo.
        session.add(ScanImage(job_id=job_b.id, file_path="/b/renomeada.jpg", file_hash="abc123"))
        with pytest.raises(IntegrityError):
            session.flush()


class TestForeignKeys:
    def test_orphan_print_is_rejected(self, session: Session) -> None:
        session.add(
            CardPrint(
                card_id=999999,
                set_code_full="XXX-001",
                set_code_normalized="XXX-001",
                set_name="Inexistente",
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()

    def test_deleting_card_cascades_to_prints(self, session: Session) -> None:
        card = make_card(session)
        make_print(session, card, "LOB-001", "Ultra Rare")
        session.commit()

        session.delete(card)
        session.commit()
        assert session.query(CardPrint).count() == 0

    def test_set_prefix_is_optional(self, session: Session) -> None:
        """Print cujo prefixo não casa com nenhum set conhecido é preservado."""
        card = make_card(session)
        card_print = make_print(session, card, "ZZZZ-EN999", "Common")
        assert card_print.set_prefix is None

    def test_set_prefix_links_to_card_set(self, session: Session) -> None:
        session.add(CardSet(set_code="LOB", set_name="Legend of Blue Eyes White Dragon"))
        card = make_card(session)
        card_print = make_print(session, card, "LOB-001", "Ultra Rare")
        card_print.set_prefix = "LOB"
        session.flush()
        assert card_print.card_set is not None
        assert card_print.card_set.set_name.startswith("Legend")


class TestCardAltNameConstraints:
    """Nomes em FR/DE/IT/PT (plano §7.1 multilíngue)."""

    def test_language_vocabulary_enforced(self, session: Session) -> None:
        card = make_card(session)
        session.add(
            CardAltName(card_id=card.id, language="ES", name="x", name_normalized="x")
        )
        with pytest.raises(IntegrityError):
            session.flush()

    def test_duplicate_language_for_same_card_is_rejected(self, session: Session) -> None:
        card = make_card(session)
        session.add(
            CardAltName(card_id=card.id, language="PT", name="a", name_normalized="a")
        )
        session.flush()
        session.add(
            CardAltName(card_id=card.id, language="PT", name="b", name_normalized="b")
        )
        with pytest.raises(IntegrityError):
            session.flush()

    def test_same_card_different_languages_coexist(self, session: Session) -> None:
        card = make_card(session)
        session.add_all(
            [
                CardAltName(card_id=card.id, language="PT", name="a", name_normalized="a"),
                CardAltName(card_id=card.id, language="FR", name="b", name_normalized="b"),
            ]
        )
        session.flush()
        assert session.query(CardAltName).count() == 2

    def test_deleting_card_cascades_to_alt_names(self, session: Session) -> None:
        card = make_card(session)
        session.add(
            CardAltName(card_id=card.id, language="PT", name="a", name_normalized="a")
        )
        session.commit()

        session.delete(card)
        session.commit()
        assert session.query(CardAltName).count() == 0
