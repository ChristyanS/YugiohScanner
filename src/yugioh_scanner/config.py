"""Configuração da aplicação — a única fonte de verdade (plano §17).

Precedência: variável de ambiente > arquivo `.env` > default.

Nada de `os.getenv` espalhado pelo código: todo módulo recebe `Settings` por
injeção ou chama `get_settings()`.
"""

from __future__ import annotations

import os
import sys
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LogFormat = Literal["console", "json"]

#: Providers que acompanham a aplicação. NÃO é um `Literal` de propósito: o
#: registry (`ocr/registry.py`) é a fonte de verdade e aceita providers
#: registrados em runtime. Fechar o tipo aqui criaria uma segunda lista para
#: manter em sincronia — e adicionar um provider passaria a exigir editar a
#: configuração, exatamente o acoplamento que o Protocol existe para evitar.
#: A validação acontece onde o nome é usado, com erro que lista os disponíveis.
BUILTIN_OCR_PROVIDERS = ("rapidocr", "tesseract", "paddle", "easyocr", "claude")

def _detect_roots() -> tuple[Path, Path]:
    """(âncora para paths relativos do usuário, raiz dos recursos empacotados).

    Rodando de código-fonte, as duas coincidem: a raiz do projeto (…/
    src/yugioh_scanner/config.py -> …/). Dentro do .exe (PyInstaller
    onefile), `RESOURCES_ROOT` é a pasta temporária onde o bundle foi
    extraído (`sys._MEIPASS`) — correta para achar `migrations/`,
    `alembic.ini` e os templates/estáticos da web, mas efêmera, então
    **não** serve para dados do usuário. Estes ficam em `%APPDATA%`, que
    sobrevive entre execuções e updates do .exe (plano da Fase de
    empacotamento web).
    """
    if getattr(sys, "frozen", False):
        resources = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
        appdata = Path(os.environ.get("APPDATA", Path.home()))
        return appdata / "YugiohScanner", resources
    root = Path(__file__).resolve().parents[2]
    return root, root


#: Âncora para resolver paths relativos do usuário (`data_path`, etc.).
#: Repo root em execução normal; `%APPDATA%/YugiohScanner` dentro do .exe.
PROJECT_ROOT, RESOURCES_ROOT = _detect_roots()

#: Teto absoluto de workers.
MAX_WORKERS = 8

#: Divisor usado no cálculo automático de workers.
#:
#: O plano previa `cpu_count - 1`. A medição em hardware real (i5-9400F, 6
#: núcleos físicos, 10 cartas) mostrou que isso desperdiça processos: o OCR
#: satura a banda de memória, não a CPU. Com 6 workers o paralelismo real é 5×,
#: mas cada carta fica 3,5× mais lenta — o ganho líquido some.
#:
#:   workers=1  wall 37,2s   throughput 0,27 cartas/s
#:   workers=2  wall 27,6s   throughput 0,36 cartas/s   <- praticamente todo o ganho
#:   workers=4  wall 29,3s   throughput 0,34 cartas/s
#:   workers=6  wall 25,2s   throughput 0,40 cartas/s
#:
#: Metade dos núcleos captura o ganho com menos memória e menos tempo de spawn.
#: Quem quiser afinar usa `--workers` ou `YGS_OCR_WORKERS`; a Fase 9 revisita
#: isso com o corpus de fotos reais.
WORKER_CORE_DIVISOR = 2


class Settings(BaseSettings):
    """Configuração validada no boot.

    Todas as variáveis usam o prefixo ``YGS_`` (ex.: ``YGS_OCR_PROVIDER``).
    A chave da Anthropic também é lida de ``ANTHROPIC_API_KEY``, sem prefixo,
    por ser o nome canônico que o SDK e outras ferramentas já usam.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="YGS_",
        # `ignore` e não `forbid`: o .env legitimamente carrega chaves sem o
        # nosso prefixo (ANTHROPIC_API_KEY), e `forbid` as rejeitaria.
        extra="ignore",
        validate_default=True,
    )

    # ------------------------------------------------------------------ dados
    data_path: Path = Path("data")
    database_url: str | None = Field(
        default=None,
        description="URL SQLAlchemy. Se ausente, derivada de data_path.",
    )
    image_cache_path: Path | None = Field(
        default=None,
        description="Cache de imagens. Se ausente, data_path/images.",
    )
    allowed_scan_roots: list[Path] = Field(
        default_factory=list,
        description="Se não vazio, só pastas sob estas raízes podem ser escaneadas.",
    )

    # -------------------------------------------------------------------- ocr
    ocr_provider: str = "rapidocr"
    ocr_fallback_provider: str = "none"
    ocr_workers: int | None = Field(
        default=None, description="None = automático (cpu_count-1, teto de 8)."
    )
    ocr_timeout_s: int = Field(default=60, gt=0)
    ocr_threads_per_worker: int | None = Field(
        default=None,
        description=(
            "Threads que cada worker de OCR pode usar. None = automático: "
            "todos os núcleos no modo serial, 1 dentro de um pool de processos."
        ),
    )
    tesseract_cmd: Path | None = None

    # -------------------------------------------------------------------- llm
    llm_model: str = "claude-opus-5"
    llm_api_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("YGS_LLM_API_KEY", "ANTHROPIC_API_KEY"),
    )
    llm_concurrency: int = Field(default=4, gt=0, le=32)
    llm_use_batches: bool = False

    # --------------------------------------------------------------- matching
    confidence_auto: float = Field(default=0.93, ge=0.0, le=1.0)
    confidence_review: float = Field(default=0.70, ge=0.0, le=1.0)
    fuzzy_cutoff: int = Field(default=70, ge=0, le=100)

    # -------------------------------------------------------------------- sync
    sync_alt_languages: list[str] = Field(
        default_factory=lambda: ["FR", "DE", "IT", "PT", "JA", "KO"],
        description=(
            "Idiomas alternativos baixados a mais no sync, para o matching "
            "reconhecer cartas fotografadas nesses idiomas. Vazio desliga. "
            "JA/KO não são documentados pela API, só confirmados ao vivo "
            "(docs/proposta-i18n-cartas-e-sets.md) — `db probe-languages` "
            "revalida a qualquer momento."
        ),
    )

    # ------------------------------------------------------ enriquecimento i18n
    #: Arquivo agregado do yaml-yugi (ADR 0012) — dump estático (~100 MB),
    #: não uma API paginada. Consumido só por `db enrich-i18n`, nunca pelo
    #: `sync` principal (docs/proposta-fontes-dados-catalogo.md: fonte
    #: opcional, isolada, nunca quebra o catálogo primário).
    #:
    #: **Não** é a branch git `aggregate` do repositório
    #: (`raw.githubusercontent.com/.../aggregate/cards.json`) — essa branch
    #: ficou travada em 2024-06-02 (confirmado ao vivo em 2026-09-13: o
    #: workflow que a publicava parou de escrever nela, embora ainda "rode
    #: com sucesso" — provavelmente publica só para o GitHub Pages agora). A
    #: URL certa é o **GitHub Pages** do projeto, que está de fato
    #: atualizado (confirmado: `Last-Modified` do mesmo dia, RA05-PT/QCAC-JA
    #: presentes para cartas que a branch git não tinha). Se algum dia isso
    #: voltar a ficar defasado, `db enrich-i18n` não vai falhar — só vai
    #: importar dados velhos de novo, em silêncio; vale checar o
    #: `Last-Modified` do header HTTP periodicamente.
    yaml_yugi_cards_url: str = "https://dawnbrandbots.github.io/yaml-yugi/cards.json"
    http_download_timeout_s: float = Field(
        default=120.0, gt=0, description="Timeout maior: o dump do yaml-yugi tem ~100 MB."
    )

    # ------------------------------------------------------------------- rede
    ygoprodeck_base_url: str = "https://db.ygoprodeck.com/api/v7"
    ygoprodeck_image_host: str = "images.ygoprodeck.com"
    http_rate_limit_per_s: float = Field(
        default=10.0,
        gt=0,
        description="Metade do limite deles (20/s), de propósito.",
    )
    http_connect_timeout_s: float = Field(default=10.0, gt=0)
    http_read_timeout_s: float = Field(default=60.0, gt=0)
    http_max_retries: int = Field(default=3, ge=0)
    http_backoff_base_s: float = Field(
        default=1.0, ge=0, description="Base do backoff exponencial entre tentativas."
    )

    # -------------------------------------------------------------------- web
    web_host: str = "127.0.0.1"
    web_port: int = Field(default=8000, gt=0, lt=65536)
    #: PNGs de scanner reais passam fácil de 10-20 MB numa página A4/Carta a
    #: 300+ DPI (medido: ~18 MB numa digitalização real) — o padrão antigo de
    #: 10 MB rejeitava scans legítimos no upload da Web.
    max_upload_mb: int = Field(default=50, gt=0)
    max_upload_files: int = Field(default=200, gt=0)
    max_image_pixels: int = Field(
        default=40_000_000, gt=0, description="Guarda contra decompression bomb."
    )

    # --------------------------------------------------------- observabilidade
    log_level: str = "INFO"
    log_format: LogFormat = "console"

    # ------------------------------------------------------------ validadores

    @field_validator("data_path", "image_cache_path", "tesseract_cmd")
    @classmethod
    def _resolve_path(cls, value: Path | None) -> Path | None:
        """Resolve caminhos relativos contra a raiz do projeto, não o cwd.

        Sem isso, `yugioh-scanner` rodado de outra pasta apontaria para um banco
        diferente — o tipo de bug que só aparece em produção.
        """
        if value is None:
            return None
        return value if value.is_absolute() else (PROJECT_ROOT / value).resolve()

    @field_validator("allowed_scan_roots")
    @classmethod
    def _resolve_roots(cls, value: list[Path]) -> list[Path]:
        return [p.resolve() for p in value]

    @field_validator("log_level")
    @classmethod
    def _valid_log_level(cls, value: str) -> str:
        level = value.upper()
        valid = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if level not in valid:
            raise ValueError(f"log_level deve ser um de {sorted(valid)}, recebido {value!r}")
        return level

    @field_validator("sync_alt_languages")
    @classmethod
    def _valid_alt_languages(cls, value: list[str]) -> list[str]:
        # Checagem de **formato** (2-3 letras), não de enumeração fechada —
        # mesma decisão do CHECK de `card_alt_name.language`
        # (db/tables.py `ck_card_alt_name_language_format`, duplicada aqui
        # sem import para `config` continuar sem depender de `db`, plano
        # §17). Motivo: a API já aceitou na prática um idioma (`ja`) que nem
        # o guia oficial nem a própria mensagem de erro dela documentam —
        # travar numa lista fixa só adia a próxima surpresa, não evita
        # (docs/proposta-i18n-cartas-e-sets.md §1.4/§1.6). Quem quer saber
        # se um código específico funciona hoje deve rodar
        # `yugioh-scanner db probe-languages`, não confiar na validação aqui.
        normalized = [lang.upper() for lang in value]
        invalid = sorted(lang for lang in normalized if not (2 <= len(lang) <= 3 and lang.isalpha()))
        if invalid:
            raise ValueError(
                f"sync_alt_languages espera códigos de 2-3 letras (ex. 'PT', 'JA'), "
                f"recebido {invalid}"
            )
        return normalized

    @field_validator("ocr_workers")
    @classmethod
    def _valid_workers(cls, value: int | None) -> int | None:
        if value is not None and value < 1:
            raise ValueError("ocr_workers deve ser >= 1 (ou omitido para automático)")
        return value

    @model_validator(mode="after")
    def _check_confidence_order(self) -> Settings:
        if self.confidence_review > self.confidence_auto:
            raise ValueError(
                "confidence_review não pode ser maior que confidence_auto "
                f"({self.confidence_review} > {self.confidence_auto})"
            )
        return self

    # -------------------------------------------------------------- derivados

    @property
    def database_path(self) -> Path:
        """Caminho do arquivo SQLite (mesmo quando `database_url` foi dado)."""
        if self.database_url is None:
            return self.data_path / "yugioh.db"
        prefix = "sqlite:///"
        if self.database_url.startswith(prefix):
            return Path(self.database_url[len(prefix) :]).resolve()
        # Outro dialeto (ex.: Postgres no futuro) não tem arquivo local.
        raise NonLocalDatabaseUrlError(self.database_url)

    @property
    def effective_database_url(self) -> str:
        if self.database_url is not None:
            return self.database_url
        return f"sqlite:///{self.database_path.as_posix()}"

    @property
    def is_sqlite(self) -> bool:
        return self.effective_database_url.startswith("sqlite")

    @property
    def images_path(self) -> Path:
        return self.image_cache_path or (self.data_path / "images")

    @property
    def uploads_path(self) -> Path:
        return self.data_path / "uploads"

    @property
    def logs_path(self) -> Path:
        return self.data_path / "logs"

    @property
    def backups_path(self) -> Path:
        return self.data_path / "backups"

    @property
    def effective_workers(self) -> int:
        """Workers para OCR local: explícito, ou metade dos núcleos (ver acima)."""
        if self.ocr_workers is not None:
            return min(self.ocr_workers, MAX_WORKERS)
        return min(MAX_WORKERS, max(1, (os.cpu_count() or 2) // WORKER_CORE_DIVISOR))

    def ensure_directories(self) -> None:
        """Cria a árvore em `data/`. Idempotente."""
        for path in (
            self.data_path,
            self.images_path,
            self.images_path / "cards",
            self.images_path / "cards_small",
            self.images_path / "sets",
            self.uploads_path,
            self.logs_path,
            self.backups_path,
        ):
            path.mkdir(parents=True, exist_ok=True)

    def masked_dump(self) -> dict[str, str]:
        """Configuração efetiva com segredos mascarados, para `config show`."""
        out: dict[str, str] = {}
        for name in sorted(type(self).model_fields):
            value = getattr(self, name)
            out[name] = mask_secret(value)
        out["effective_database_url"] = self.effective_database_url
        out["effective_workers"] = str(self.effective_workers)
        return out


class NonLocalDatabaseUrlError(ValueError):
    """`database_path` foi pedido para uma URL que não é um arquivo local."""

    def __init__(self, url: str) -> None:
        super().__init__(f"A URL {url!r} não aponta para um arquivo SQLite local.")


def mask_secret(value: object) -> str:
    """Mostra só o suficiente de um segredo para identificá-lo."""
    if value is None:
        return ""
    if isinstance(value, SecretStr):
        raw = value.get_secret_value()
        if not raw:
            return ""
        return f"{raw[:7]}***{raw[-4:]}" if len(raw) > 14 else "***"
    return str(value)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Instância única, carregada uma vez por processo."""
    return Settings()


def reset_settings_cache() -> None:
    """Descarta a instância em cache (usado por testes)."""
    get_settings.cache_clear()
