"""Hierarquia de exceções da aplicação.

Princípio (plano §16): *erro por imagem é dado, não exceção*. Falhas que afetam
uma única imagem viram registro no banco (`ScanImage.status`); só o que impede o
comando inteiro de prosseguir sobe como exceção daqui.

Toda exceção carrega uma `user_message` — o texto que a CLI/Web mostram sem
stack trace — e opcionalmente uma `hint` com a ação corretiva.
"""

from __future__ import annotations


class YugiohScannerError(Exception):
    """Base de todos os erros previstos da aplicação."""

    exit_code: int = 2

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.user_message = message
        self.hint = hint

    def __str__(self) -> str:
        if self.hint:
            return f"{self.user_message}\n  → {self.hint}"
        return self.user_message


# --------------------------------------------------------------- configuração


class ConfigError(YugiohScannerError):
    """Configuração ausente, inválida ou contraditória."""

    exit_code = 1


# --------------------------------------------------------------------- banco


class DatabaseError(YugiohScannerError):
    """Base dos problemas de banco de dados."""


class DatabaseNotInitializedError(DatabaseError):
    """O arquivo do banco não existe ainda."""

    def __init__(self, path: str) -> None:
        super().__init__(
            f"Banco de dados não encontrado em {path}.",
            hint="Execute `yugioh-scanner init` para criá-lo e baixar o catálogo.",
        )


class SchemaOutdatedError(DatabaseError):
    """O schema do banco está atrás das migrações disponíveis."""

    def __init__(self, current: str | None, head: str) -> None:
        super().__init__(
            f"O schema do banco está desatualizado (atual: {current or 'nenhum'}, "
            f"esperado: {head}).",
            hint="Execute `yugioh-scanner db upgrade`.",
        )


class DatabaseCorruptedError(DatabaseError):
    """`PRAGMA integrity_check` falhou."""


# ------------------------------------------------------------- API externa


class ApiError(YugiohScannerError):
    """Base das falhas ao falar com a YGOPRODeck."""


class ApiUnavailableError(ApiError):
    """A API não respondeu depois de todas as tentativas."""


class ApiRateLimitedError(ApiError):
    """A API devolveu 429 mesmo após respeitar o `Retry-After`."""


class ApiResponseError(ApiError):
    """A API respondeu algo que não conseguimos interpretar."""


# ------------------------------------------------------------------ scanner


class ScanError(YugiohScannerError):
    """Falha que impede o job de scan inteiro (não uma imagem isolada)."""


class InvalidScanPathError(ScanError):
    """A pasta pedida não existe, não é pasta, ou está fora da allowlist."""


class OcrProviderError(ScanError):
    """O provider de OCR não pôde ser criado ou inicializado."""


class OcrProviderUnavailableError(OcrProviderError):
    """O provider existe mas suas dependências não estão instaladas."""

    def __init__(self, provider: str, package: str) -> None:
        super().__init__(
            f"O provider de OCR '{provider}' não está disponível.",
            hint=f'Instale-o com: pip install -e ".[{package}]"',
        )


class ScanResultNotFoundError(ScanError):
    """Nenhum resultado de scan com o ID pedido."""

    def __init__(self, result_id: int) -> None:
        super().__init__(f"Resultado de scan #{result_id} não encontrado.")
        self.result_id = result_id


class ScanResultAlreadyAppliedError(ScanError):
    """Esta leitura já contribuiu para a coleção — confirmar de novo duplicaria."""

    def __init__(self, result_id: int) -> None:
        super().__init__(
            f"O resultado #{result_id} já foi aplicado à coleção anteriormente.",
            hint="Nada a fazer: a cópia já está contabilizada.",
        )
        self.result_id = result_id


class JobNotFoundError(ScanError):
    """Nenhum job de scan com o ID pedido."""

    def __init__(self, job_id: int) -> None:
        super().__init__(f"Job de scan #{job_id} não encontrado.")
        self.job_id = job_id


# ---------------------------------------------------------------- coleção


class CollectionError(YugiohScannerError):
    """Operação inválida sobre a coleção."""


class CardNotFoundError(CollectionError):
    """Nenhuma carta do catálogo corresponde ao que foi pedido."""


class AmbiguousCardError(CollectionError):
    """Vários candidatos igualmente plausíveis — precisa de desempate humano."""

    def __init__(self, query: str, candidates: list[str]) -> None:
        listed = "\n  - ".join(candidates)
        super().__init__(
            f"'{query}' corresponde a mais de uma carta:\n  - {listed}",
            hint="Refine o nome ou informe o --set-code.",
        )
        self.candidates = candidates


class CollectionItemNotFoundError(CollectionError):
    """Nenhum item de coleção com o ID pedido."""

    def __init__(self, item_id: int) -> None:
        super().__init__(f"Item de coleção #{item_id} não encontrado.")
        self.item_id = item_id


class PrintNotFoundForCardError(CollectionError):
    """O set code informado não corresponde a nenhum print daquela carta.

    Diferente do matching por OCR (que aceita `NULL` de bom grado quando o
    código não valida — plano §7.2), aqui o usuário digitou o código à mão:
    aceitar em silêncio um código que não pertence à carta esconderia um erro
    de digitação em vez de avisar sobre ele.
    """

    def __init__(self, card_name: str, set_code: str) -> None:
        super().__init__(
            f"'{set_code}' não é um print conhecido de '{card_name}'.",
            hint="Confira o código ou omita --set-code para deixar o set indefinido.",
        )


# -------------------------------------------------------------- exportação


class ExportError(YugiohScannerError):
    """Falha ao exportar a coleção."""


class UnknownExportProfileError(ExportError):
    def __init__(self, profile: str, available: list[str]) -> None:
        super().__init__(
            f"Perfil de exportação desconhecido: '{profile}'.",
            hint=f"Disponíveis: {', '.join(available)}",
        )
