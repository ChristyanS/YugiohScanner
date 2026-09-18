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


# --------------------------------------------------------- enriquecimento i18n


class EnrichmentError(YugiohScannerError):
    """Falha do enriquecimento opcional (`db enrich-i18n`, ADR 0012).

    Categoria própria, não `ApiError`: uma falha aqui nunca deve impedir o
    `sync` principal (YGOPRODeck) nem qualquer outro comando de funcionar —
    a fonte é explicitamente best-effort (docs/proposta-fontes-dados-catalogo.md).
    """


class EnrichmentSourceUnavailableError(EnrichmentError):
    """Não foi possível baixar ou interpretar o dataset de enriquecimento."""

    def __init__(self, source: str, reason: str) -> None:
        super().__init__(
            f"Não foi possível obter o dataset de enriquecimento em {source}: {reason}",
            hint="O catálogo principal (YGOPRODeck) continua íntegro; tente novamente mais tarde.",
        )


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


class LlmApiKeyMissingError(OcrProviderError):
    """Provider de OCR por LLM configurado, mas sem chave de API.

    Distinto de `OcrProviderUnavailableError`: o pacote está instalado, só
    falta a credencial. Quem chama (a cascata de fallback, plano §6.3)
    trata isto como "degradar em silêncio para manual" (ADR 0004) — nunca
    como falha do scan inteiro.
    """

    def __init__(self, provider: str) -> None:
        super().__init__(
            f"O provider de OCR '{provider}' precisa de uma chave de API.",
            hint="Defina ANTHROPIC_API_KEY (ou YGS_LLM_API_KEY) no ambiente ou no .env.",
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


class ScanImageNotFoundError(ScanError):
    """Nenhuma imagem de scan com o ID pedido."""

    def __init__(self, image_id: int) -> None:
        super().__init__(f"Imagem de scan #{image_id} não encontrada.")
        self.image_id = image_id


# --------------------------------------------------------------- captura


class CaptureError(YugiohScannerError):
    """Falha ao capturar uma página de um scanner/impressora físico.

    Categoria irmã de `ScanError`, não subclasse dela: `ScanError` documenta
    falha que impede um `ScanJob` inteiro, e uma falha de captura acontece
    *antes* de qualquer `ScanJob` existir (plano §22).
    """


class ScannerUnsupportedPlatformError(CaptureError):
    """Captura por scanner só existe via WIA (Windows) nesta versão."""

    def __init__(self) -> None:
        super().__init__(
            "Captura por scanner só está disponível no Windows (WIA).",
            hint="Use `scan PASTA` com fotos tiradas por outro meio.",
        )


class ScannerUnavailableError(CaptureError):
    """O backend existe mas `pywin32` não está instalado."""

    def __init__(self) -> None:
        super().__init__(
            "Suporte a scanner/impressora não está instalado.",
            hint='Instale com: pip install -e ".[wia]"',
        )


class ScannerBackendUnknownError(CaptureError):
    """Nome de backend de captura sem implementação registrada."""

    def __init__(self, name: str, available: list[str]) -> None:
        super().__init__(
            f"Backend de captura desconhecido: {name!r}.",
            hint=f"Disponíveis: {', '.join(available)}",
        )


class NoScannerDeviceFoundError(CaptureError):
    """WIA não encontrou nenhum scanner/impressora conectado."""

    def __init__(self) -> None:
        super().__init__(
            "Nenhum scanner/impressora WIA encontrado.",
            hint="Confira se o dispositivo está ligado e instalado no Windows.",
        )


class ScanCaptureFailedError(CaptureError):
    """A automação COM/WIA falhou durante a captura em si."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"Falha ao capturar da página: {reason}")


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


class SetAlreadyExistsError(CollectionError):
    """Já existe um set com este código — cadastro manual é create, não upsert.

    Um typo de cadastro manual não pode sobrescrever silenciosamente um set
    que já veio do sync (plano de revisão: cadastro manual de set).
    """

    def __init__(self, set_code: str) -> None:
        super().__init__(
            f"Já existe um set cadastrado com o código '{set_code}'.",
            hint="Use o set já existente em vez de cadastrar de novo.",
        )


class InvalidSetDataError(CollectionError):
    """Código ou nome de set ausente/vazio no cadastro manual."""


# ------------------------------------------------------------- preferências


class SettingsError(YugiohScannerError):
    """Base dos erros de preferências do usuário (`app_setting`)."""


class InvalidSettingValueError(SettingsError):
    """Valor fora do vocabulário aceito para esta preferência."""

    def __init__(self, key: str, value: str, allowed: tuple[str, ...]) -> None:
        super().__init__(
            f"Valor inválido para '{key}': '{value}'.",
            hint=f"Valores aceitos: {', '.join(allowed)}",
        )


# -------------------------------------------------------------- deck builder


class DeckError(YugiohScannerError):
    """Base dos erros do Deck Builder."""


class DeckNotFoundError(DeckError):
    """Nenhum deck com o ID pedido."""

    def __init__(self, deck_id: int) -> None:
        super().__init__(f"Deck #{deck_id} não encontrado.")
        self.deck_id = deck_id


class DeckValidationError(DeckError):
    """Uma adição/remoção violaria uma regra de construção de deck.

    Bloqueante (levantado por `DeckService.add_card`) — diferente de
    `DeckService.validate_deck`, que nunca levanta exceção e só reporta o
    que está fora das regras (painel de legalidade do deck inteiro).
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)


# -------------------------------------------------------------- exportação


class ExportError(YugiohScannerError):
    """Falha ao exportar a coleção."""


class UnknownExportProfileError(ExportError):
    def __init__(self, profile: str, available: list[str]) -> None:
        super().__init__(
            f"Perfil de exportação desconhecido: '{profile}'.",
            hint=f"Disponíveis: {', '.join(available)}",
        )


# ------------------------------------------------------------------------ web


class UploadError(YugiohScannerError):
    """Falha ao receber arquivos enviados pelo navegador (plano §21)."""


class TooManyUploadFilesError(UploadError):
    def __init__(self, count: int, limit: int) -> None:
        super().__init__(
            f"{count} arquivos enviados, o limite é {limit} por envio.",
            hint="Envie em lotes menores.",
        )


class UploadTooLargeError(UploadError):
    def __init__(self, limit_mb: int) -> None:
        super().__init__(
            f"Um dos arquivos passa do limite de {limit_mb} MB.",
            hint="Redimensione a foto ou envie em resolução menor.",
        )
