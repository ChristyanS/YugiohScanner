"""Cliente HTTP do dataset agregado `yaml-yugi` (ADR 0012).

Diferença fundamental para `ygoprodeck/client.py`: aqui não há paginação nem
rate limit a respeitar — é **um arquivo estático** (~94 MB), baixado por
inteiro. A única otimização que importa é não baixá-lo de novo quando nada
mudou, por isso o suporte a `ETag`/`If-None-Match` (o mesmo papel que
`checkDBVer.php` cumpre para a YGOPRODeck, só que via cabeçalho HTTP padrão
em vez de um endpoint próprio).
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx
from pydantic import ValidationError

from ...config import Settings
from ...errors import EnrichmentSourceUnavailableError
from ...logging_setup import get_logger
from .schemas import YamlYugiCard

log = get_logger(__name__)

USER_AGENT = "yugioh-scanner/0.1 (+https://github.com/ChristyanS/YugiohScanner)"


@dataclass
class FetchResult:
    """Resultado de `fetch_cards`.

    `not_modified=True` significa "o `etag` que você tinha ainda é válido" —
    o chamador deve manter os dados locais como estão; `cards` vem vazio
    nesse caso, nunca use como sinal de "dataset vazio".
    """

    cards: list[YamlYugiCard]
    etag: str | None
    not_modified: bool = False


class YamlYugiClient:
    """Acesso somente-leitura ao arquivo agregado.

    O cliente httpx pode ser injetado — é assim que os testes usam `respx`
    sem tocar na rede (mesmo padrão de `YgoProDeckClient`).
    """

    def __init__(self, settings: Settings, *, client: httpx.Client | None = None) -> None:
        self._settings = settings
        self._owns_client = client is None
        self._client = client or httpx.Client(
            timeout=httpx.Timeout(
                connect=settings.http_connect_timeout_s,
                read=settings.http_download_timeout_s,
                write=settings.http_download_timeout_s,
                pool=settings.http_connect_timeout_s,
            ),
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            follow_redirects=True,
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> YamlYugiClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def fetch_cards(self, *, etag: str | None = None) -> FetchResult:
        """Baixa (ou confirma que não precisa baixar) o dataset agregado.

        Qualquer falha vira `EnrichmentSourceUnavailableError` — categoria
        própria (não `ApiError`) porque quem chama trata isso como
        best-effort, nunca como motivo para abortar outra coisa.
        """
        url = self._settings.yaml_yugi_cards_url
        headers = {"If-None-Match": etag} if etag else {}

        try:
            response = self._client.get(url, headers=headers)
        except httpx.HTTPError as exc:
            raise EnrichmentSourceUnavailableError(url, str(exc)) from exc

        if response.status_code == 304:
            log.info("yaml_yugi.not_modified", etag=etag)
            return FetchResult(cards=[], etag=etag, not_modified=True)

        if response.status_code != 200:
            raise EnrichmentSourceUnavailableError(url, f"HTTP {response.status_code}")

        try:
            payload = response.json()
        except ValueError as exc:
            raise EnrichmentSourceUnavailableError(url, "resposta não é JSON válido") from exc

        if not isinstance(payload, list):
            raise EnrichmentSourceUnavailableError(url, "formato inesperado (esperava uma lista)")

        try:
            cards = [YamlYugiCard.model_validate(item) for item in payload]
        except ValidationError as exc:
            raise EnrichmentSourceUnavailableError(
                url, f"não foi possível interpretar: {exc}"
            ) from exc

        new_etag = response.headers.get("ETag")
        log.info("yaml_yugi.fetched", count=len(cards), etag=new_etag)
        return FetchResult(cards=cards, etag=new_etag)
