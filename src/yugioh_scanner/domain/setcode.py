"""Parsing de set codes (plano §8 e §7.2, camadas 1 e 2).

Um set code identifica uma impressão específica: `LOB-001`, `SDK-001`,
`MAGO-EN001`, `RA01-EN001`, `MP24-EN001`, `MVP1-ENV04`. Não existe formato
único, e a parte numérica nem sempre é numérica.

Este módulo faz o parsing e a canonicalização (camadas 1 e 2). A **camada 3** —
validar o código contra os prefixos e códigos reais do banco — vive em
`matching/`, porque depende de dados. A separação é intencional: aqui não há
I/O, então tudo é testável sem banco.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: Códigos de região que aparecem entre o prefixo e o número.
#: Ordenados por tamanho decrescente para o casamento ser guloso ("EN" antes
#: de "E", senão "EN001" viraria região "E" + número "N001").
REGION_CODES: tuple[str, ...] = (
    "EN",  # inglês (TCG)
    "FR",
    "DE",
    "IT",
    "PT",
    "SP",
    "ES",
    "JP",
    "JA",
    "KR",
    "AE",  # Asian-English
    "AS",
    "TC",
    "SC",
    "NA",
    "EU",
    "E",  # prints europeus antigos
    "A",
    "F",
    "G",
    "I",
    "P",
    "S",
)

_DASHES = str.maketrans({"‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "―": "-", "−": "-"})

#: Sujeira que o OCR adiciona nas bordas do código.
_TRIM = " \t\r\n.,;:|/\\[](){}*\"'"

#: Prefixo: 2 a 6 alfanuméricos. Sufixo: 2 a 6 alfanuméricos (pode conter
#: letras — "ENV04", "ENSV4"). O separador é obrigatório aqui; a ausência dele
#: é tratada em `_split_without_separator`.
_WITH_SEPARATOR = re.compile(r"^(?P<prefix>[A-Z0-9]{2,6})-(?P<tail>[A-Z0-9]{2,7})$")

#: Sem separador: letras seguidas de dígitos, com pelo menos 2 de cada lado.
_WITHOUT_SEPARATOR = re.compile(r"^(?P<prefix>[A-Z]{2,6})(?P<tail>[0-9]{2,4})$")


@dataclass(frozen=True, slots=True)
class SetCode:
    """Um set code decomposto.

    `normalized` é a forma canônica usada para busca no banco — sempre
    `PREFIXO-REGIÃONÚMERO`, em maiúsculas, com um único hífen.
    """

    raw: str
    prefix: str
    region: str | None
    number: str

    @property
    def normalized(self) -> str:
        return f"{self.prefix}-{self.region or ''}{self.number}"

    def __str__(self) -> str:
        return self.normalized


def _clean(raw: str) -> str:
    """Maiúsculas, traços unificados, lixo das bordas removido."""
    return raw.translate(_DASHES).strip(_TRIM).upper().replace(" ", "")


def _split_region(tail: str) -> tuple[str | None, str]:
    """Separa o código de região do número, quando houver.

    Duas regras diferentes, ditadas pelos dados reais do catálogo:

    * **Região de duas letras** (`EN`, `PT`, `JP`…): o resto pode conter letras,
      porque existem números como `V04` (`MVP1-ENV04`) e `TKN` (`SR03-ENTKN`).
    * **Região de uma letra** (`E`, `F`, `S`… dos prints europeus antigos): o
      resto precisa ser **só dígitos**. Sem essa restrição, `IOC-SE1` (Special
      Edition) seria lido como região "S" + número "E1", que está errado — caso
      encontrado nos 44.517 prints reais do catálogo.
    """
    for region in REGION_CODES:
        if not tail.startswith(region):
            continue
        remainder = tail[len(region) :]
        if len(remainder) < 2:
            continue
        if len(region) == 1 and not remainder.isdigit():
            continue
        return region, remainder
    return None, tail


def parse_set_code(raw: str) -> SetCode | None:
    """Decompõe um set code. Devolve None se o texto não tem essa forma.

    >>> parse_set_code("LOB-001")
    SetCode(raw='LOB-001', prefix='LOB', region=None, number='001')
    >>> parse_set_code("MAGO-EN001").normalized
    'MAGO-EN001'
    >>> parse_set_code("mvp1-env04").normalized
    'MVP1-ENV04'
    >>> parse_set_code("Blue-Eyes") is None
    True
    """
    if not raw:
        return None

    cleaned = _clean(raw)
    if not cleaned:
        return None

    match = _WITH_SEPARATOR.match(cleaned)
    if match is None:
        match = _WITHOUT_SEPARATOR.match(cleaned)
    if match is None:
        return None

    tail = match.group("tail")
    region, number = _split_region(tail)

    # Sem região reconhecida, o sufixo precisa ter dígito: é isso que separa um
    # código (`LOB-001`) de uma palavra com hífen (`BLUE-EYES`).
    if region is None and not any(ch.isdigit() for ch in tail):
        return None

    return SetCode(raw=raw, prefix=match.group("prefix"), region=region, number=number)


def normalize_set_code(raw: str) -> str:
    """Forma canônica para gravar em `card_print.set_code_normalized`.

    Quando o texto não casa com nenhum formato conhecido, devolve a versão
    limpa em maiúsculas — nunca inventa estrutura.

    >>> normalize_set_code("lob-001")
    'LOB-001'
    >>> normalize_set_code("MVP1-ENV04")
    'MVP1-ENV04'
    >>> normalize_set_code("qualquer coisa")
    'QUALQUERCOISA'
    """
    parsed = parse_set_code(raw)
    return parsed.normalized if parsed else _clean(raw)


def extract_prefix(raw: str) -> str | None:
    """Só o prefixo do set (`LOB-EN001` → `LOB`), ou None."""
    parsed = parse_set_code(raw)
    return parsed.prefix if parsed else None


def looks_like_set_code(raw: str) -> bool:
    """Heurística barata para decidir se vale tentar o parsing completo."""
    return parse_set_code(raw) is not None


# ------------------------------------------------- camada 2: correção de OCR

#: No **número**, o OCR troca letras por dígitos parecidos.
_LETTER_TO_DIGIT = str.maketrans({"O": "0", "I": "1", "L": "1", "S": "5", "B": "8", "Z": "2"})

#: No **prefixo** vale o inverso: o dígito é que costuma ser a leitura errada.
_DIGIT_TO_LETTER = str.maketrans({"0": "O", "1": "I", "5": "S", "8": "B", "2": "Z"})


def correction_variants(raw: str, *, limit: int = 6) -> list[str]:
    """Variantes plausíveis de um código lido com erro, da mais provável à menos.

    A correção é **dirigida por posição**, não cega: no número trocamos letras
    por dígitos (`LOB-O01` → `LOB-001`), no prefixo fazemos o inverso
    (`L0B-001` → `LOB-001`). Aplicar os dois mapas em tudo geraria lixo como
    `1OB-OO1`.

    O primeiro elemento é sempre a leitura original: quem chama valida os
    candidatos contra o banco na ordem e para no primeiro que existir — nada
    aqui decide sozinho qual é o código certo (camada 3).

    >>> correction_variants("LOB-O01")[:2]
    ['LOB-O01', 'LOB-001']
    >>> correction_variants("L0B-001")[:2]
    ['L0B-001', 'LOB-001']
    """
    cleaned = _clean(raw)
    if not cleaned:
        return []

    variants: list[str] = [cleaned]

    def add(candidate: str) -> None:
        if candidate and candidate not in variants:
            variants.append(candidate)

    parsed = parse_set_code(cleaned)
    if parsed is not None:
        prefix, region, number = parsed.prefix, parsed.region or "", parsed.number
        fixed_number = number.translate(_LETTER_TO_DIGIT)
        fixed_prefix = prefix.translate(_DIGIT_TO_LETTER)

        add(f"{prefix}-{region}{fixed_number}")
        add(f"{fixed_prefix}-{region}{number}")
        add(f"{fixed_prefix}-{region}{fixed_number}")
    else:
        # Sem estrutura reconhecida, ainda vale tentar consertar o todo: o erro
        # pode ser justamente o que impede o parsing.
        add(cleaned.translate(_LETTER_TO_DIGIT))
        add(cleaned.translate(_DIGIT_TO_LETTER))
        # O OCR come o hífen com frequência; tentar sem ele às vezes destrava.
        if "-" not in cleaned:
            for position in range(2, min(7, len(cleaned) - 1)):
                add(f"{cleaned[:position]}-{cleaned[position:]}")

    return variants[:limit]
