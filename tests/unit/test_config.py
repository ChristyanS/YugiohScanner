"""Testes de `Settings` (plano §17)."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from yugioh_scanner.config import MAX_WORKERS, PROJECT_ROOT, Settings, mask_secret


def test_defaults_are_usable_without_any_env() -> None:
    settings = Settings()
    assert settings.ocr_provider == "rapidocr"
    assert settings.ocr_fallback_provider == "none"
    assert settings.web_host == "127.0.0.1", "bind local é o default: não há autenticação"
    assert settings.http_rate_limit_per_s <= 20.0, "nunca ultrapassar o limite da API"


def test_relative_paths_resolve_against_project_root_not_cwd() -> None:
    """Rodar a CLI de outra pasta não pode apontar para outro banco."""
    settings = Settings(data_path=Path("data"))
    assert settings.data_path == (PROJECT_ROOT / "data").resolve()
    assert settings.data_path.is_absolute()


def test_database_url_derived_from_data_path(tmp_path: Path) -> None:
    settings = Settings(data_path=tmp_path)
    assert settings.database_path == tmp_path / "yugioh.db"
    assert settings.effective_database_url.startswith("sqlite:///")


def test_explicit_database_url_wins(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'outro.db').as_posix()}"
    settings = Settings(data_path=tmp_path, database_url=url)
    assert settings.effective_database_url == url
    assert settings.database_path.name == "outro.db"


def test_derived_paths(tmp_path: Path) -> None:
    settings = Settings(data_path=tmp_path)
    assert settings.images_path == tmp_path / "images"
    assert settings.uploads_path == tmp_path / "uploads"
    assert settings.logs_path == tmp_path / "logs"
    assert settings.backups_path == tmp_path / "backups"


def test_image_cache_path_overrides_default(tmp_path: Path) -> None:
    custom = tmp_path / "cache"
    settings = Settings(data_path=tmp_path, image_cache_path=custom)
    assert settings.images_path == custom


def test_ensure_directories_is_idempotent(tmp_path: Path) -> None:
    settings = Settings(data_path=tmp_path / "data")
    settings.ensure_directories()
    settings.ensure_directories()
    assert (settings.images_path / "cards_small").is_dir()
    assert settings.uploads_path.is_dir()


class TestWorkers:
    def test_auto_workers_leaves_a_core_free_and_respects_cap(self) -> None:
        workers = Settings().effective_workers
        assert 1 <= workers <= MAX_WORKERS

    def test_explicit_workers_is_capped(self) -> None:
        assert Settings(ocr_workers=999).effective_workers == MAX_WORKERS

    def test_explicit_workers_below_cap_is_respected(self) -> None:
        assert Settings(ocr_workers=3).effective_workers == 3

    def test_zero_workers_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Settings(ocr_workers=0)


class TestValidation:
    def test_review_threshold_above_auto_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="confidence_review"):
            Settings(confidence_auto=0.80, confidence_review=0.95)

    def test_equal_thresholds_allowed(self) -> None:
        settings = Settings(confidence_auto=0.9, confidence_review=0.9)
        assert settings.confidence_auto == settings.confidence_review

    def test_confidence_outside_range_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Settings(confidence_auto=1.5)

    def test_log_level_is_normalized_to_upper(self) -> None:
        assert Settings(log_level="debug").log_level == "DEBUG"

    def test_invalid_log_level_rejected(self) -> None:
        with pytest.raises(ValidationError, match="log_level"):
            Settings(log_level="verbose")

    def test_ocr_provider_is_open_ended(self) -> None:
        """O registry é a fonte de verdade dos providers, não a configuração.

        Um nome desconhecido é aceito aqui e rejeitado (com a lista dos
        disponíveis) no momento em que alguém tenta criá-lo — o que permite
        registrar providers em runtime sem editar `config.py`.
        """
        assert Settings(ocr_provider="meu-plugin").ocr_provider == "meu-plugin"


class TestSyncAltLanguages:
    """Validação por **formato**, não por enumeração fechada (docs/
    proposta-i18n-cartas-e-sets.md §1.6): a API já aceitou na prática um
    idioma (`ja`) que nem ela mesma documenta como válido."""

    def test_default_includes_undocumented_but_verified_languages(self) -> None:
        assert Settings().sync_alt_languages == ["FR", "DE", "IT", "PT", "JA", "KO"]

    def test_lowercase_is_normalized_to_upper(self) -> None:
        assert Settings(sync_alt_languages=["pt", "ja"]).sync_alt_languages == ["PT", "JA"]

    def test_language_outside_default_vocabulary_is_accepted(self) -> None:
        """Não é uma enumeração fechada — `db probe-languages` é quem diz se
        a API aceita de verdade, não este validador."""
        assert Settings(sync_alt_languages=["ES"]).sync_alt_languages == ["ES"]

    def test_empty_list_disables_alt_names(self) -> None:
        assert Settings(sync_alt_languages=[]).sync_alt_languages == []

    @pytest.mark.parametrize("bad", ["P", "PORT", "P1", "PT-BR"])
    def test_invalid_format_rejected(self, bad: str) -> None:
        with pytest.raises(ValidationError, match="sync_alt_languages"):
            Settings(sync_alt_languages=[bad])


class TestEnvironment:
    def test_env_var_with_prefix_is_read(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("YGS_OCR_PROVIDER", "tesseract")
        assert Settings().ocr_provider == "tesseract"

    def test_anthropic_key_read_without_prefix(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """O nome canônico que o SDK já usa precisa funcionar."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-teste-1234567890")
        settings = Settings()
        assert settings.llm_api_key is not None
        assert settings.llm_api_key.get_secret_value() == "sk-ant-teste-1234567890"

    def test_prefixed_key_wins_over_anthropic_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-generico")
        monkeypatch.setenv("YGS_LLM_API_KEY", "sk-ant-especifico")
        settings = Settings()
        assert settings.llm_api_key is not None
        assert settings.llm_api_key.get_secret_value() == "sk-ant-especifico"


class TestSecretMasking:
    def test_secret_is_masked(self) -> None:
        masked = mask_secret(SecretStr("sk-ant-api03-abcdefghijklmnop"))
        assert "abcdefghij" not in masked
        assert masked.startswith("sk-ant-")
        assert masked.endswith("mnop")

    def test_short_secret_fully_masked(self) -> None:
        assert mask_secret(SecretStr("curto")) == "***"

    def test_none_and_empty(self) -> None:
        assert mask_secret(None) == ""
        assert mask_secret(SecretStr("")) == ""

    def test_masked_dump_never_leaks_the_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-segredo-absoluto")
        dump = Settings().masked_dump()
        assert "segredo-absoluto" not in "".join(dump.values())
        assert "effective_database_url" in dump
