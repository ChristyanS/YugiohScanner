"""Resolução de set code contra o banco — a camada 3 do plano §7.2.

É esta camada que decide de verdade. As camadas 1 e 2 (`domain/setcode.py`)
apenas *propõem* candidatos; aqui cada um é confrontado com os ~646 prefixos e
~44.500 códigos reais do catálogo. Um código inventado pelo OCR simplesmente
não casa e é descartado.

A regra que nunca se quebra: **na dúvida, `NULL`**. Uma carta entrar na coleção
sem set definido é um estado previsto e corrigível; entrar com o set errado é
dado corrompido silenciosamente.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.tables import CardPrint, CardSet
from ..domain.setcode import correction_variants, normalize_set_code, parse_set_code
from ..logging_setup import get_logger

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class PrintMatch:
    """Um print concreto do catálogo."""

    print_id: int
    card_id: int
    set_code_full: str
    set_name: str
    rarity: str | None

    def as_dict(self) -> dict[str, object]:
        return {
            "print_id": self.print_id,
            "card_id": self.card_id,
            "set_code": self.set_code_full,
            "set_name": self.set_name,
            "rarity": self.rarity,
        }


@dataclass
class CodeResolution:
    """O que sabemos sobre o código lido depois de confrontá-lo com o banco."""

    #: Texto original vindo do OCR.
    raw: str
    #: Variante que efetivamente casou (pode diferir de `raw` — foi corrigida).
    matched_code: str | None = None
    prints: list[PrintMatch] = field(default_factory=list)
    #: O prefixo existe no catálogo de sets, mesmo que o código completo não.
    prefix_known: bool = False
    #: A regex reconheceu a forma, ainda que nada tenha casado no banco.
    parsed: bool = False
    #: Região que o código **lido** carregava (`SetCode.region`), antes de
    #: qualquer substituição por inglês — plano de idiomas, continuação
    #: (docs/adr/0009). É o sinal mais direto de idioma que existe: o código
    #: impresso na carta física. Fica preenchido mesmo quando o print daquela
    #: região não existe no catálogo e a resolução caiu para o equivalente em
    #: inglês (`matched_code` vira "-EN-", mas `detected_region` continua
    #: sendo o que a carta realmente diz).
    detected_region: str | None = None

    @property
    def validated(self) -> bool:
        """Casou com pelo menos um print real."""
        return bool(self.prints)

    @property
    def card_id(self) -> int | None:
        """Carta apontada pelo código — só quando todos os prints concordam.

        Uma mesma carta pode ter dois prints no mesmo código (raridades
        diferentes); isso continua identificando a carta. Códigos que apontam
        para cartas diferentes não identificam nada.
        """
        if not self.prints:
            return None
        card_ids = {print_match.card_id for print_match in self.prints}
        return card_ids.pop() if len(card_ids) == 1 else None

    @property
    def print_id(self) -> int | None:
        """Print concreto — só quando não há ambiguidade de raridade.

        Com duas raridades no mesmo código, escolher uma seria inventar dado.
        A carta fica identificada, e a raridade vai para a revisão.
        """
        return self.prints[0].print_id if len(self.prints) == 1 else None

    @property
    def score(self) -> float:
        """Peso do código no cálculo de confiança (plano §7.4)."""
        if self.validated:
            return 1.0
        if self.parsed:
            # A forma é de um set code, mas o banco não conhece: pode ser um
            # print novo, pode ser leitura errada. Meio-termo honesto.
            return 0.5
        return 0.0

    def as_dict(self) -> dict[str, object]:
        return {
            "raw": self.raw,
            "matched_code": self.matched_code,
            "validated": self.validated,
            "prefix_known": self.prefix_known,
            "prints": [print_match.as_dict() for print_match in self.prints],
            "detected_region": self.detected_region,
        }


class PrintResolver:
    """Confronta códigos lidos com o catálogo local."""

    def __init__(self, session: Session) -> None:
        self.session = session
        self._prefixes: set[str] | None = None

    def known_prefixes(self) -> set[str]:
        """Prefixos reais do catálogo, carregados uma vez por resolver."""
        if self._prefixes is None:
            self._prefixes = set(self.session.scalars(select(CardSet.set_code)).all())
        return self._prefixes

    def resolve(self, raw_code: str | None) -> CodeResolution:
        """Tenta identificar o print a partir do texto lido.

        Testa a leitura original e depois as variantes corrigidas, **na ordem**,
        e para na primeira que existir no banco. Nada é aceito sem casar.

        Quando nada valida e o código carregava uma região diferente de
        inglês, tenta a mesma carta com região `EN` antes de desistir — plano
        de idiomas, continuação (docs/adr/0009): a maioria dos produtos não
        tem print distinto por idioma (a carta física usa o código em inglês
        mesmo impressa em outro idioma); só um punhado (PT/OTS) tem código
        próprio, e nem todo produto desses foi sincronizado. `detected_region`
        guarda o que a carta realmente diz, para a coleção registrar o idioma
        certo mesmo usando o print em inglês.
        """
        if not raw_code or not raw_code.strip():
            return CodeResolution(raw=raw_code or "")

        resolution = CodeResolution(raw=raw_code)
        initial_parse = parse_set_code(raw_code)
        if initial_parse is not None and (
            initial_parse.region is None or len(initial_parse.region) >= 2
        ):
            # Região de 1 letra (prints europeus antigos, `PSV-E088`) só é
            # confiável depois de validada contra o catálogo abaixo — achado
            # real (Scan #11): o OCR troca "T" por "1" dentro de uma região
            # de 2 letras ("PT002" -> "P1002"), e "P" sozinho bate na mesma
            # regra de região de 1 letra (resto só com dígitos). Sem esta
            # guarda, `detected_region="P"` vencia `best.language="PT"` do
            # nome (`matching/engine.py`, `or` é guloso: string não-vazia
            # ganha de qualquer jeito) e gravava um idioma de 1 letra em
            # `ScanResult.detected_language` — que o CHECK de
            # `card_print_override` rejeita (2-3 letras), travando a
            # confirmação da revisão inteira sem nenhum erro visível.
            resolution.detected_region = initial_parse.region

        variants = correction_variants(raw_code)
        for variant in variants:
            parsed = parse_set_code(variant)
            if parsed is not None:
                resolution.parsed = True
                if parsed.prefix in self.known_prefixes():
                    resolution.prefix_known = True

            normalized = normalize_set_code(variant)
            prints = self._find_prints(normalized)
            if prints:
                resolution.matched_code = normalized
                resolution.prints = prints
                if parsed is not None:
                    # Validado pelo catálogo agora — mesmo uma região de 1
                    # letra pode ser confiada (o `PrintMatch` encontrado
                    # prova que não era ruído de OCR).
                    resolution.detected_region = parsed.region
                if variant != variants[0]:
                    log.debug("matching.code_corrected", raw=raw_code, matched=normalized)
                return resolution

        if (
            initial_parse is not None
            and resolution.detected_region
            and resolution.detected_region != "EN"
        ):
            en_code = normalize_set_code(f"{initial_parse.prefix}-EN{initial_parse.number}")
            prints = self._find_prints(en_code)
            if prints:
                resolution.matched_code = en_code
                resolution.prints = prints
                log.debug(
                    "matching.code_language_fallback",
                    raw=raw_code,
                    detected_region=resolution.detected_region,
                    matched=en_code,
                )

        return resolution

    def _find_prints(self, normalized_code: str) -> list[PrintMatch]:
        rows = self.session.execute(
            select(
                CardPrint.id,
                CardPrint.card_id,
                CardPrint.set_code_full,
                CardPrint.set_name,
                CardPrint.rarity,
            ).where(CardPrint.set_code_normalized == normalized_code)
        ).all()
        return [
            PrintMatch(
                print_id=row.id,
                card_id=row.card_id,
                set_code_full=row.set_code_full,
                set_name=row.set_name,
                rarity=row.rarity,
            )
            for row in rows
        ]

    def rarities_for_set(self, card_id: int, set_code_full: str) -> list[str]:
        """Raridades catalogadas para essa carta nesse set — a picklist que a
        revisão/coleção mostra em vez de deixar o usuário digitar às cegas
        (ex.: Dark Magician Girl em RA05 → ["Ultra Rare", "Starlight Rare"]).
        """
        prints = self.prints_for_card(card_id, set_code_full)
        seen: set[str] = set()
        rarities: list[str] = []
        for print_match in prints:
            if print_match.rarity and print_match.rarity not in seen:
                seen.add(print_match.rarity)
                rarities.append(print_match.rarity)
        return rarities

    def prints_for_card(self, card_id: int, code: str) -> list[PrintMatch]:
        """Prints daquela carta cujo código bate — usado para desempatar.

        Quando nome e código apontam para a mesma carta mas há mais de um print
        (raridades), é isto que a tela de revisão mostra para o usuário escolher.
        """
        normalized = normalize_set_code(code)
        return [
            print_match
            for print_match in self._find_prints(normalized)
            if print_match.card_id == card_id
        ]
