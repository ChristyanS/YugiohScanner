"""Cliente HTTP da YGOPRODeck (plano §0.1, §16 e §20.4).

Três obrigações que não são opcionais:

1. **Rate limit.** O limite deles é 20 req/s; usamos 10 de propósito.
2. **Retry com backoff**, respeitando `Retry-After` em 429.
3. **Paginação.** São ~14.500 cartas; baixar tudo de uma vez custa centenas de
   MB de RAM. Páginas de 1.000 mantêm a memória constante e dão progresso real.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import httpx

from ..config import Settings
from ..errors import ApiRateLimitedError, ApiResponseError, ApiUnavailableError
from ..logging_setup import get_logger
from .schemas import ApiCard, ApiCardPage, ApiDbVersion, ApiPageMeta, ApiSet

log = get_logger(__name__)

USER_AGENT = "yugioh-scanner/0.1 (+https://github.com/ChristyanS/YugiohScanner)"

#: Tamanho de página. Grande o bastante para poucas requisições, pequeno o
#: bastante para a memória não explodir.
DEFAULT_PAGE_SIZE = 1000

#: Carta usada como sonda em `probe_language` — precisa ter tradução
#: conhecida em qualquer idioma que se queira testar. Dark Magician/Mago
#: Negro tem tradução confirmada em fr/de/it/pt/ja/ko (2026-09-12).
PROBE_CARD_ID = 46986414


class RateLimiter:
    """Token bucket simples e thread-safe.

    Espaça as requisições no tempo em vez de disparar rajadas — é o que evita
    entrar na lista de bloqueio deles.
    """

    def __init__(self, rate_per_second: float) -> None:
        self._min_interval = 1.0 / rate_per_second if rate_per_second > 0 else 0.0
        self._lock = threading.Lock()
        self._next_allowed = 0.0

    def acquire(self) -> None:
        if self._min_interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            wait = self._next_allowed - now
            if wait > 0:
                time.sleep(wait)
                now = time.monotonic()
            self._next_allowed = now + self._min_interval


class YgoProDeckClient:
    """Acesso somente-leitura à API v7.

    O cliente httpx pode ser injetado — é assim que os testes usam `respx` sem
    tocar na rede.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        self._settings = settings
        self._limiter = RateLimiter(settings.http_rate_limit_per_s)
        self._owns_client = client is None
        self._client = client or httpx.Client(
            base_url=settings.ygoprodeck_base_url,
            timeout=httpx.Timeout(
                connect=settings.http_connect_timeout_s,
                read=settings.http_read_timeout_s,
                write=settings.http_read_timeout_s,
                pool=settings.http_connect_timeout_s,
            ),
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            follow_redirects=True,
        )

    # ------------------------------------------------------------ ciclo de vida

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> YgoProDeckClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------- HTTP

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """GET com rate limit, retry e tradução de erros.

        Retenta em 429, 5xx e falhas de conexão; **não** retenta em 4xx, que
        significa que o pedido está errado e tentar de novo não vai ajudar.
        """
        attempts = self._settings.http_max_retries + 1
        last_error: Exception | None = None

        for attempt in range(1, attempts + 1):
            self._limiter.acquire()
            try:
                response = self._client.get(path, params=params)
            except httpx.HTTPError as exc:
                last_error = exc
                log.warning("api.connection_error", path=path, attempt=attempt, error=str(exc))
                if attempt < attempts:
                    time.sleep(self._backoff(attempt))
                    continue
                raise ApiUnavailableError(
                    f"Não foi possível falar com a YGOPRODeck após {attempts} tentativa(s): {exc}",
                    hint="Verifique sua conexão e tente novamente.",
                ) from exc

            if response.status_code == 429:
                delay = self._retry_after(response, attempt)
                log.warning("api.rate_limited", path=path, attempt=attempt, sleep_s=delay)
                if attempt < attempts:
                    time.sleep(delay)
                    continue
                raise ApiRateLimitedError(
                    "A YGOPRODeck recusou por excesso de requisições (429).",
                    hint="Reduza YGS_HTTP_RATE_LIMIT_PER_S e tente de novo em alguns minutos.",
                )

            if response.status_code >= 500:
                log.warning("api.server_error", path=path, status=response.status_code)
                if attempt < attempts:
                    time.sleep(self._backoff(attempt))
                    continue
                raise ApiUnavailableError(
                    f"A YGOPRODeck respondeu {response.status_code} após {attempts} tentativa(s)."
                )

            if response.status_code >= 400:
                # Erro nosso: retentar não muda nada.
                raise ApiResponseError(
                    f"A YGOPRODeck rejeitou a requisição ({response.status_code}): "
                    f"{response.text[:200]}"
                )

            try:
                return response.json()
            except ValueError as exc:
                raise ApiResponseError(f"A resposta de {path} não é JSON válido.") from exc

        raise ApiUnavailableError(str(last_error))  # pragma: no cover - inalcançável

    def _backoff(self, attempt: int) -> float:
        """Backoff exponencial: base, 2×base, 4×base…"""
        return self._settings.http_backoff_base_s * float(2 ** (attempt - 1))

    def _retry_after(self, response: httpx.Response, attempt: int) -> float:
        header = response.headers.get("Retry-After")
        if header:
            try:
                return min(float(header), 60.0)
            except ValueError:
                pass
        return self._backoff(attempt)

    # ---------------------------------------------------------------- endpoints

    def check_db_version(self) -> ApiDbVersion:
        """Versão do catálogo deles. É o que evita re-sincronizar à toa."""
        payload = self._get("/checkDBVer.php")
        if isinstance(payload, list):
            if not payload:
                raise ApiResponseError("checkDBVer.php devolveu uma lista vazia.")
            payload = payload[0]
        return ApiDbVersion.model_validate(payload)

    def fetch_sets(self) -> list[ApiSet]:
        """Catálogo completo de sets (`cardsets.php`)."""
        payload = self._get("/cardsets.php")
        if not isinstance(payload, list):
            raise ApiResponseError("cardsets.php devolveu um formato inesperado.")
        return [ApiSet.model_validate(item) for item in payload]

    def fetch_card_page(
        self, offset: int, num: int = DEFAULT_PAGE_SIZE, *, language: str | None = None
    ) -> ApiCardPage:
        """Uma página de `cardinfo.php`, com `misc=yes` para datas e konami_id.

        `language` pede o catálogo traduzido (`fr`, `de`, `it`, `pt` — os
        únicos que a API aceita; qualquer outro valor ela rejeita com 400).
        Sem ele, vem o catálogo padrão em inglês. O `id` (passcode) é o mesmo
        em todo idioma — é o que permite ligar o nome traduzido à carta certa
        sem uma segunda chave.
        """
        params: dict[str, Any] = {"num": num, "offset": offset, "misc": "yes"}
        if language is not None:
            params["language"] = language
        payload = self._get("/cardinfo.php", params=params)
        if isinstance(payload, list):  # pragma: no cover - defensivo
            return ApiCardPage(data=payload)
        return ApiCardPage.model_validate(payload)

    def iter_cards(
        self, page_size: int = DEFAULT_PAGE_SIZE, *, language: str | None = None
    ) -> Iterator[tuple[list[ApiCard], ApiPageMeta | None]]:
        """Percorre o catálogo inteiro, página a página.

        Emite `(cartas, meta)` para que o chamador possa mostrar progresso sem
        precisar saber como a paginação funciona. `language` repassa para
        `fetch_card_page` — o catálogo traduzido pagina normalmente.
        """
        offset = 0
        while True:
            page = self.fetch_card_page(offset=offset, num=page_size, language=language)
            if not page.data:
                return
            yield page.data, page.meta

            meta = page.meta
            if meta is not None:
                if meta.pages_remaining <= 0 or meta.next_page_offset is None:
                    return
                offset = meta.next_page_offset
            else:  # pragma: no cover - API sem meta
                if len(page.data) < page_size:
                    return
                offset += page_size

    def fetch_card_by_name(self, name: str) -> ApiCard | None:
        """Consulta pontual por nome exato — usada em diagnóstico, não no sync."""
        try:
            payload = self._get("/cardinfo.php", params={"name": name, "misc": "yes"})
        except ApiResponseError:
            return None  # a API devolve 400 quando o nome não existe
        page = ApiCardPage.model_validate(payload)
        return page.data[0] if page.data else None

    def probe_language(self, language: str, *, probe_card_id: int = PROBE_CARD_ID) -> bool:
        """A API aceita `language` agora, de verdade?

        Não confia no guia oficial nem na mensagem de erro que a própria API
        devolve para um valor inválido — os dois citam só `fr/de/it/pt`, mas
        `ja` e `ko` funcionam na prática (verificado em 2026-09-12,
        docs/proposta-i18n-cartas-e-sets.md §1.4). Como não há changelog
        público para esse tipo de comportamento não-documentado, a única
        forma confiável de saber é perguntar à API — o que este método faz,
        contra uma carta com tradução conhecida.
        """
        try:
            self._get(
                "/cardinfo.php", params={"id": probe_card_id, "language": language.lower()}
            )
            return True
        except ApiResponseError:
            return False

    def probe_languages(
        self, languages: Sequence[str], *, probe_card_id: int = PROBE_CARD_ID
    ) -> dict[str, bool]:
        """`probe_language` para vários candidatos — usado por `db probe-languages`."""
        return {
            language: self.probe_language(language, probe_card_id=probe_card_id)
            for language in languages
        }

    # -------------------------------------------------------------- imagens

    def download_image(self, url: str, destination: Path) -> int:
        """Baixa uma imagem para o disco. Devolve os bytes gravados.

        A política deles é explícita: baixar **uma vez** e servir do cache local
        (plano §0.2). Este método é o único ponto que toca `images.ygoprodeck.com`,
        e quem chama é responsável por não pedir duas vezes o mesmo arquivo.
        """
        host = httpx.URL(url).host
        if host != self._settings.ygoprodeck_image_host:
            # Guarda contra SSRF: só baixamos do host de imagens conhecido.
            raise ApiResponseError(f"Host de imagem não permitido: {host!r}")

        self._limiter.acquire()
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self._client.stream("GET", url) as response:
                if response.status_code != 200:
                    raise ApiResponseError(f"Falha ao baixar {url}: HTTP {response.status_code}")
                written = 0
                temporary = destination.with_suffix(destination.suffix + ".part")
                with temporary.open("wb") as handle:
                    for chunk in response.iter_bytes(chunk_size=64 * 1024):
                        handle.write(chunk)
                        written += len(chunk)
                # Renomear só no fim: um download interrompido nunca vira um
                # arquivo corrompido no cache.
                temporary.replace(destination)
                return written
        except httpx.HTTPError as exc:
            raise ApiUnavailableError(f"Falha ao baixar {url}: {exc}") from exc
