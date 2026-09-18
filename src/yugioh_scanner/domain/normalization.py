"""Normalização de nomes de cartas (plano §7.1).

Duas funções puras, sem I/O, e a regra que faz tudo funcionar: **o mesmo
pipeline roda na ingestão e no OCR**. Nunca comparamos strings normalizadas por
regras diferentes.

`normalize_strict` produz a forma que vai para o banco e para o índice.
`normalize_fuzzy` é uma canonicalização mais agressiva usada só na comparação:
ela colapsa os pares que o OCR confunde (0/O, 1/I, 5/S…) **dos dois lados**, de
modo que "WH1TE" e "WHITE" viram a mesma coisa. Note que não é correção — é
canonicalização; por isso é seguro aplicá-la a dígitos legítimos, desde que
aplicada também ao catálogo.
"""

from __future__ import annotations

import re
import unicodedata

#: Aspas e travessões tipográficos que a API e o OCR usam de formas diferentes.
_TYPOGRAPHIC = {
    "‘": "'",  # ‘
    "’": "'",  # ’
    "‛": "'",
    "“": '"',  # “
    "”": '"',  # ”
    "–": "-",  # –
    "—": "-",  # —
    "‐": "-",
    "‑": "-",
    "‒": "-",
    "―": "-",
    "−": "-",  # −
    " ": " ",  # espaço não-quebrável
}

_TYPO_TABLE = str.maketrans(_TYPOGRAPHIC)

#: Sobrevivem ao strict: letras, dígitos, espaço, hífen e apóstrofo.
_STRICT_ALLOWED = re.compile(r"[^a-z0-9 '\-]")
_WHITESPACE = re.compile(r"\s+")

#: Classes de confusão do OCR, colapsadas em um representante único.
#: Cada classe precisa mapear **todos** os seus membros, não só o dígito: o
#: OCR lê "Blue" como "B1ue" e também como "Biue", então `l`, `1` e `i` têm de
#: cair no mesmo símbolo. Aplicado aos dois lados da comparação, o que mantém
#: dígitos legítimos ("Number 39") casando entre si.
_OCR_CONFUSION = str.maketrans(
    {
        "0": "o",  # classe {0, O}
        "1": "i",  # classe {1, I, l}
        "l": "i",
        "5": "s",  # classe {5, S}
        "8": "b",  # classe {8, B}
        "2": "z",  # classe {2, Z}
    }
)

#: Sequências que o OCR funde ou separa. Ordem importa: "rn" antes de "m".
_OCR_SEQUENCES = (
    ("rn", "m"),
    ("vv", "w"),
)


def normalize_strict(raw: str) -> str:
    """Forma canônica para armazenar e indexar.

    >>> normalize_strict("Blue-Eyes White Dragon")
    'blue-eyes white dragon'
    >>> normalize_strict("Harpie's  Feather Duster")
    "harpie's feather duster"
    >>> normalize_strict("Ojama Yellow ")
    'ojama yellow'
    """
    text = raw.translate(_TYPO_TABLE)
    # NFKD separa o acento da letra; removemos o acento **apagando-o**, não
    # trocando por espaço — senão "Pôt" viraria "po t" e quebraria a busca.
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.casefold()
    text = _STRICT_ALLOWED.sub(" ", text)
    text = _WHITESPACE.sub(" ", text)
    return text.strip()


def normalize_fuzzy(raw: str) -> str:
    """Forma agressiva usada só na comparação — nunca armazenada.

    >>> normalize_fuzzy("BLUE-EYES WH1TE DRAGON")
    'biueeyes white dragon'
    >>> normalize_fuzzy("Blue-Eyes White Dragon")
    'biueeyes white dragon'
    >>> normalize_fuzzy("Harpie's Feather Duster")
    'harpies feather duster'
    """
    text = normalize_strict(raw)
    text = text.translate(_OCR_CONFUSION)
    for source, target in _OCR_SEQUENCES:
        text = text.replace(source, target)
    # Hífens e apóstrofos: o OCR os perde e os inventa com igual frequência.
    text = text.replace("-", "").replace("'", "")
    return _WHITESPACE.sub(" ", text).strip()


def normalize_for_display(raw: str) -> str:
    """Limpeza leve: só colapsa espaços e unifica tipográficos.

    Usada quando o texto vai ser mostrado ao usuário (por exemplo o OCR bruto na
    tela de revisão) e destruir a caixa atrapalharia a leitura.
    """
    return _WHITESPACE.sub(" ", raw.translate(_TYPO_TABLE)).strip()


def strip_accents_only(raw: str) -> str:
    """Remove só os acentos, preservando pontuação e caixa (plano de idioma
    global: colação PT_BR de ordenação alfabética).

    Deliberadamente mais leve que `normalize_strict`: aquela também colapsa
    hífen/apóstrofo para espaço, o que é certo para busca mas mudaria a
    ordem relativa de nomes com pontuação de verdade (ex.: "Harpie's" vs
    "Harpies") se fosse reaproveitada aqui.
    """
    text = raw.translate(_TYPO_TABLE)
    text = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in text if not unicodedata.combining(ch))
