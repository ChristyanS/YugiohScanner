"""Parsing de passcode — o "Card ID" impresso no canto inferior-esquerdo.

Identifica a carta diretamente: é a chave primária do catálogo (`Card.id`,
"passcode" nos termos da YGOPRODeck — plano §0.3.2/§0.4). Puramente numérico
(5 a 8 dígitos), o que faz do OCR aqui um problema bem mais simples que ler um
nome: só 10 classes de caractere, contra 26+ letras, acentos e os alfabetos
JA/KO que este catálogo já reconhece. É por isso que faz sentido tratá-lo como
a fonte mais forte de identidade da carta, com nome e set code entrando como
evidência de apoio/desempate (`matching/engine.py::_from_passcode`).

A validação — o passcode precisa existir de verdade no catálogo — vive em
`matching/passcode.py`, mesma separação de `domain/setcode.py`: aqui não há
I/O, então tudo é testável sem banco.
"""

from __future__ import annotations

import re

#: Mesmo mapa de `domain/setcode.py::_LETTER_TO_DIGIT` — mesma fonte impressa
#: na carta física, mesmas confusões de OCR entre dígito e letra parecida.
_LETTER_TO_DIGIT = str.maketrans({"O": "0", "I": "1", "L": "1", "S": "5", "B": "8", "Z": "2"})

#: Sujeira que o OCR adiciona nas bordas do texto lido.
_TRIM = " \t\r\n.,;:|/\\[](){}*\"'#"

#: Passcodes reais do catálogo têm 5 a 8 dígitos (cartas muito antigas do TCG
#: usam menos que os 8 dígitos padrão de hoje). Fora dessa faixa, a leitura
#: quase certamente não é um passcode.
_MIN_DIGITS = 5
_MAX_DIGITS = 8


#: Passcode "colado" no texto vizinho pelo OCR (achado real, calibração
#: contra fotos de celular: a ROI precisa de folga vertical por causa da
#: imprecisão de `detect_card_bounds` entre fotos, e essa folga às vezes
#: pega a linha de baixo — "1ª Edição"/copyright — juntada numa caixa só
#: pelo motor de OCR, ex. "41739381 1Edicao"). Extrai o maior run de dígitos
#: plausível em vez de descartar a leitura inteira.
_DIGIT_RUN = re.compile(rf"\d{{{_MIN_DIGITS},{_MAX_DIGITS}}}")


def clean_passcode(raw: str | None) -> str | None:
    """Corrige confusões de OCR letra↔dígito e valida a forma.

    Devolve só a sequência de dígitos corrigida, ou `None` quando a leitura
    não parece um passcode de jeito nenhum (comprimento fora da faixa 5-8, ou
    nenhum run de dígitos plausível sobrevive). Nunca decide se o número
    existe de verdade no catálogo — isso é a camada 3 (`matching/passcode.py`).

    >>> clean_passcode("89631139")
    '89631139'
    >>> clean_passcode("896311З9")  # 'З' cirílico não é confusão conhecida
    >>> clean_passcode("8963ll39")  # 'l' minúsculo -> "L" -> "1" após upper()
    '89631139'
    >>> clean_passcode("41739381 1Edicao")  # OCR juntou a linha de baixo
    '41739381'
    >>> clean_passcode("") is None
    True
    """
    if not raw:
        return None

    cleaned = raw.strip(_TRIM).upper().replace(" ", "")
    if not cleaned:
        return None

    corrected = cleaned.translate(_LETTER_TO_DIGIT)
    if corrected.isdigit():
        return corrected if _MIN_DIGITS <= len(corrected) <= _MAX_DIGITS else None

    # Mistura de dígitos com texto (não só dígitos com comprimento errado,
    # esse caso já foi tratado acima) — o passcode de verdade é o run mais
    # comprido; o texto vizinho contribui no máximo um dígito solto (o "1"
    # de "1ª Edição").
    runs = _DIGIT_RUN.findall(corrected)
    return max(runs, key=len) if runs else None
