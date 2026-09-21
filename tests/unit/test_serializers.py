"""`job_to_dict`: total de cartas e percentuais (pedido do usuário).

`ScanJob.processed` conta **fotos**; numa foto de grade (ADR 0011) uma única
foto rende N cartas. Achado real do usuário: Scan #1 mostrava "61
processadas" mas o total de cartas era maior — o dashboard/detalhe do scan
precisa de um número que conte cartas, não fotos.
"""

from __future__ import annotations

import datetime as dt

from yugioh_scanner.db.tables import ScanJob
from yugioh_scanner.web.serializers import job_to_dict


def _job(**overrides: object) -> ScanJob:
    defaults: dict[str, object] = dict(
        id=1,
        folder_path="/fotos",
        ocr_provider="fake",
        workers=1,
        status="done",
        auto_enabled=True,
        require_set=True,
        require_rarity=False,
        total_images=61,
        processed=61,
        skipped=0,
        auto_added=0,
        pending=0,
        failed=0,
        started_at=dt.datetime(2026, 1, 1),
        finished_at=None,
        error=None,
    )
    defaults.update(overrides)
    return ScanJob(**defaults)


class TestTotalCards:
    def test_counts_cards_not_photos_on_a_grid_scan(self) -> None:
        job = _job(processed=61, auto_added=180, pending=20, failed=5)
        payload = job_to_dict(job)
        assert payload["total_cards"] == 205
        assert payload["total_cards"] > job.processed

    def test_percentages_add_up_to_one_hundred(self) -> None:
        job = _job(auto_added=70, pending=20, failed=10)
        payload = job_to_dict(job)
        assert payload["pct_auto"] == 70
        assert payload["pct_pending"] == 20
        assert payload["pct_failed"] == 10

    def test_percentages_are_none_without_any_cards_yet(self) -> None:
        job = _job(auto_added=0, pending=0, failed=0)
        payload = job_to_dict(job)
        assert payload["total_cards"] == 0
        assert payload["pct_auto"] is None
        assert payload["pct_pending"] is None
        assert payload["pct_failed"] is None


class TestScanPolicyFields:
    def test_exposes_the_effective_policy_captured_on_the_job(self) -> None:
        job = _job(auto_enabled=False, require_set=True, require_rarity=True)
        payload = job_to_dict(job)
        assert payload["auto_enabled"] is False
        assert payload["require_set"] is True
        assert payload["require_rarity"] is True
