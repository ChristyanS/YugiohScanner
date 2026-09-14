"""Repositório de scans (plano §13): a mecânica da idempotência."""

from __future__ import annotations

from sqlalchemy.orm import Session

from yugioh_scanner.db.tables import Card, CollectionItem
from yugioh_scanner.repositories.scans import ScanRepository


def make_card(session: Session, card_id: int = 1) -> Card:
    card = Card(id=card_id, name="X", name_normalized="x", type="Normal Monster", desc="")
    session.add(card)
    session.flush()
    return card


class TestJobLifecycle:
    def test_create_and_finish(self, session: Session) -> None:
        repo = ScanRepository(session)
        job = repo.create_job("/pasta", "fake", 4)
        assert job.status == "running"
        assert job.id is not None

        repo.finish_job(job, status="done")
        assert job.status == "done"
        assert job.finished_at is not None

    def test_recent_jobs_ordered_newest_first(self, session: Session) -> None:
        repo = ScanRepository(session)
        first = repo.create_job("/a", "fake", 1)
        second = repo.create_job("/b", "fake", 1)
        recent = repo.recent_jobs()
        assert recent[0].id == second.id
        assert recent[1].id == first.id


class TestHashIdempotency:
    def test_unknown_hash_is_not_found(self, session: Session) -> None:
        assert ScanRepository(session).find_by_hash("abc") is None

    def test_known_hashes_filters_a_batch(self, session: Session) -> None:
        repo = ScanRepository(session)
        job = repo.create_job("/x", "fake", 1)
        repo.create_image(
            job.id, file_path="/x/a.jpg", file_hash="hash-a", file_size=10, status="ok"
        )

        known = repo.known_hashes(["hash-a", "hash-b", "hash-c"])
        assert known == {"hash-a"}

    def test_known_hashes_of_empty_batch_is_empty(self, session: Session) -> None:
        assert ScanRepository(session).known_hashes([]) == set()

    def test_create_image_then_find_by_hash(self, session: Session) -> None:
        repo = ScanRepository(session)
        job = repo.create_job("/x", "fake", 1)
        image = repo.create_image(
            job.id, file_path="/x/a.jpg", file_hash="hash-a", file_size=10, status="ok"
        )
        found = repo.find_by_hash("hash-a")
        assert found is not None
        assert found.id == image.id

    def test_update_image_reading_overwrites_latest_ocr(self, session: Session) -> None:
        """`--reprocess` atualiza a leitura mais recente na própria linha."""
        repo = ScanRepository(session)
        job = repo.create_job("/x", "fake", 1)
        image = repo.create_image(
            job.id,
            file_path="/x/a.jpg",
            file_hash="hash-a",
            file_size=10,
            status="ok",
            ocr_raw={"texts": {"name": [{"text": "Old", "confidence": 0.5}]}},
        )
        repo.update_image_reading(
            image,
            status="ok",
            ocr_raw={"texts": {"name": [{"text": "New", "confidence": 0.9}]}},
            ocr_ms=42,
        )
        assert image.ocr_raw["texts"]["name"][0]["text"] == "New"
        assert image.ocr_ms == 42


class TestResultsForJob:
    """`GET /scan/{id}` (achado real do usuário): a mesma `ScanImage`
    reaparece em jobs diferentes (--reprocess, ou qualquer novo scan de uma
    pasta já vista), e cada job precisa ver só os `ScanResult` que ele
    mesmo produziu — não só os do job que descobriu o arquivo primeiro."""

    def test_scopes_by_the_job_that_produced_the_result(self, session: Session) -> None:
        repo = ScanRepository(session)
        card = make_card(session)
        first_job = repo.create_job("/x", "fake", 1)
        image = repo.create_image(
            first_job.id, file_path="/x/a.jpg", file_hash="h", file_size=1, status="ok"
        )
        repo.create_result(
            image.id,
            job_id=first_job.id,
            card_id=card.id,
            card_print_id=None,
            ocr_name_raw="X",
            ocr_code_raw=None,
            name_score=1.0,
            code_score=0.0,
            confidence=1.0,
            margin=1.0,
            candidates=None,
            decision="auto",
        )

        # --reprocess: mesma `ScanImage` (achada pelo hash, `job_id` original
        # intacto), mas um `ScanResult` novo pertence ao job **atual**.
        second_job = repo.create_job("/x", "fake", 1)
        repo.create_result(
            image.id,
            job_id=second_job.id,
            card_id=card.id,
            card_print_id=None,
            ocr_name_raw="X",
            ocr_code_raw=None,
            name_score=1.0,
            code_score=0.0,
            confidence=1.0,
            margin=1.0,
            candidates=None,
            decision="auto",
        )

        assert len(repo.results_for_job(first_job.id)) == 1
        assert len(repo.results_for_job(second_job.id)) == 1


class TestApplicationGuard:
    """`has_applied_result` é o que impede `--reprocess` de duplicar quantidade."""

    def test_fresh_image_has_no_applied_result(self, session: Session) -> None:
        repo = ScanRepository(session)
        job = repo.create_job("/x", "fake", 1)
        image = repo.create_image(
            job.id, file_path="/x/a.jpg", file_hash="h", file_size=1, status="ok"
        )
        assert repo.has_applied_result(image.id) is False

    def test_unapplied_result_does_not_count(self, session: Session) -> None:
        repo = ScanRepository(session)
        job = repo.create_job("/x", "fake", 1)
        card = make_card(session)
        image = repo.create_image(
            job.id, file_path="/x/a.jpg", file_hash="h", file_size=1, status="ok"
        )
        repo.create_result(
            image.id,
            job_id=job.id,
            card_id=card.id,
            card_print_id=None,
            ocr_name_raw="X",
            ocr_code_raw=None,
            name_score=0.5,
            code_score=0.0,
            confidence=0.5,
            margin=0.5,
            candidates=None,
            decision="pending",
        )
        assert repo.has_applied_result(image.id) is False

    def test_applied_result_is_detected(self, session: Session) -> None:
        repo = ScanRepository(session)
        job = repo.create_job("/x", "fake", 1)
        card = make_card(session)
        image = repo.create_image(
            job.id, file_path="/x/a.jpg", file_hash="h", file_size=1, status="ok"
        )
        collection_item = CollectionItem(card_id=card.id, quantity=1)
        session.add(collection_item)
        session.flush()

        result = repo.create_result(
            image.id,
            job_id=job.id,
            card_id=card.id,
            card_print_id=None,
            ocr_name_raw="X",
            ocr_code_raw=None,
            name_score=1.0,
            code_score=0.0,
            confidence=1.0,
            margin=1.0,
            candidates=None,
            decision="auto",
        )
        repo.mark_applied(result, collection_item_id=collection_item.id)
        assert repo.has_applied_result(image.id) is True
        assert result.applied is True
        assert result.decided_at is not None


class TestPendingQueue:
    def test_counts_and_lists_pending(self, session: Session) -> None:
        repo = ScanRepository(session)
        job = repo.create_job("/x", "fake", 1)
        card = make_card(session)
        image = repo.create_image(
            job.id, file_path="/x/a.jpg", file_hash="h", file_size=1, status="ok"
        )
        repo.create_result(
            image.id,
            job_id=job.id,
            card_id=card.id,
            card_print_id=None,
            ocr_name_raw="X",
            ocr_code_raw=None,
            name_score=0.8,
            code_score=0.0,
            confidence=0.8,
            margin=0.3,
            candidates=None,
            decision="pending",
        )
        assert repo.count_pending() == 1
        assert len(repo.pending_results()) == 1

    def test_applied_results_are_not_pending(self, session: Session) -> None:
        repo = ScanRepository(session)
        job = repo.create_job("/x", "fake", 1)
        card = make_card(session)
        image = repo.create_image(
            job.id, file_path="/x/a.jpg", file_hash="h", file_size=1, status="ok"
        )
        collection_item = CollectionItem(card_id=card.id, quantity=1)
        session.add(collection_item)
        session.flush()

        result = repo.create_result(
            image.id,
            job_id=job.id,
            card_id=card.id,
            card_print_id=None,
            ocr_name_raw="X",
            ocr_code_raw=None,
            name_score=1.0,
            code_score=0.0,
            confidence=1.0,
            margin=1.0,
            candidates=None,
            decision="auto",
        )
        repo.mark_applied(result, collection_item_id=collection_item.id)
        assert repo.count_pending() == 0
