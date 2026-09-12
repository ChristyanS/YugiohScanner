"""`probe_language`/`probe_languages` (docs/proposta-i18n-cartas-e-sets.md).

Não confiam na doc/mensagem de erro da API sobre quais idiomas são aceitos —
perguntam de verdade. Aqui, "de verdade" é o mock: o que importa é que 200
vira `True` e 400 vira `False`, sem propagar a exceção.
"""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
import respx

from yugioh_scanner.config import Settings
from yugioh_scanner.ygoprodeck.client import PROBE_CARD_ID, YgoProDeckClient

BASE_URL = "https://db.ygoprodeck.com/api/v7"


@pytest.fixture
def client(settings: Settings) -> Iterator[YgoProDeckClient]:
    http_client = YgoProDeckClient(settings, client=httpx.Client(base_url=BASE_URL))
    try:
        yield http_client
    finally:
        http_client.close()


def _mock_language_support(router: respx.MockRouter, *, accepted: set[str]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        language = request.url.params.get("language")
        if language in accepted:
            return httpx.Response(200, json={"data": [{"id": PROBE_CARD_ID, "name": "x"}]})
        return httpx.Response(400, json={"error": "No card matching your query was found"})

    router.get("/cardinfo.php").mock(side_effect=handler)


class TestProbeLanguage:
    def test_accepted_language_returns_true(self, client: YgoProDeckClient) -> None:
        with respx.mock(base_url=BASE_URL) as router:
            _mock_language_support(router, accepted={"ja"})
            assert client.probe_language("ja") is True

    def test_rejected_language_returns_false_without_raising(
        self, client: YgoProDeckClient
    ) -> None:
        with respx.mock(base_url=BASE_URL) as router:
            _mock_language_support(router, accepted={"ja"})
            assert client.probe_language("es") is False

    def test_is_case_insensitive_on_input(self, client: YgoProDeckClient) -> None:
        with respx.mock(base_url=BASE_URL) as router:
            _mock_language_support(router, accepted={"ja"})
            assert client.probe_language("JA") is True


class TestProbeLanguages:
    def test_reports_each_candidate_independently(self, client: YgoProDeckClient) -> None:
        with respx.mock(base_url=BASE_URL) as router:
            _mock_language_support(router, accepted={"fr", "de", "ja", "ko"})
            results = client.probe_languages(["FR", "DE", "IT", "PT", "JA", "KO", "ES"])

        assert results == {
            "FR": True,
            "DE": True,
            "IT": False,
            "PT": False,
            "JA": True,
            "KO": True,
            "ES": False,
        }
