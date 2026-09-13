"""Print equivalente num idioma-alvo — a contrapartida de `resolver.py`.

`PrintResolver` (resolver.py) vai de **código lido** para print. Isto vai de
**idioma detectado** para print: dado que já sabemos qual carta e qual print
o OCR/matching resolveu, existe uma impressão dessa mesma carta, mesmo
número, num idioma diferente?

A resposta, no catálogo real sincronizado (ver docs/adr/0009), é quase
sempre "não" — Konami não emite código de set distinto para DE/FR/IT no TCG
moderno; cartas físicas nesses idiomas trazem o **mesmo** código do inglês.
A única exceção real são os produtos "OP" (OTS Tournament Pack), que têm
prints `PT` genuínos ao lado dos `EN` (`OP13-EN006` / `OP13-PT006`, mesmo
número, mesma raridade). `SiblingPrintLanguageResolver` reflete exatamente
isso: procura a linha irmã que o catálogo já tem, nunca inventa um código.

Desenhado como `Protocol` de propósito — o dia em que existir uma base
própria de traduções de print, uma segunda implementação lê de lá com a
mesma assinatura, sem tocar em quem chama (`ScanService`, revisão web).
"""

from __future__ import annotations

from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.tables import CardPrint
from ..repositories.print_override import PrintOverrideRepository


class PrintLanguageResolver(Protocol):
    def resolve(
        self, session: Session, card_id: int, current_print: CardPrint | None, language: str
    ) -> CardPrint | None:
        """Print da mesma carta, mesmo número de coleção, no idioma pedido.

        Devolve `None` quando não existe um print distinto nesse idioma —
        que é o caso comum (DE/FR/IT): o print atual já é o correto,
        independente do idioma impresso na carta física.
        """
        ...


class SiblingPrintLanguageResolver:
    """Implementação de hoje: consulta `card_print` por
    `(card_id, set_prefix, number, region=language)`.

    Sem heurística de string sobre o código — só existe resultado quando o
    catálogo sincronizado já tem a linha (plano §7.1 multilíngue estendido).
    """

    def resolve(
        self, session: Session, card_id: int, current_print: CardPrint | None, language: str
    ) -> CardPrint | None:
        if not language or language == "EN":
            return None  # nada para converter: inglês é o print padrão
        if current_print is None or current_print.set_prefix is None:
            return None  # sem prefixo de set conhecido, não há âncora pra buscar a irmã

        candidates = list(
            session.scalars(
                select(CardPrint).where(
                    CardPrint.card_id == card_id,
                    CardPrint.set_prefix == current_print.set_prefix,
                    CardPrint.number == current_print.number,
                    CardPrint.region == language,
                )
            )
        )
        if not candidates:
            return None
        for candidate in candidates:
            if candidate.rarity == current_print.rarity:
                return candidate
        # Nunca visto nos dados reais (25 pares EN/PT conferidos, raridade
        # sempre igual), mas se a raridade não bater com nenhuma, a primeira
        # ainda é uma resposta melhor que nenhuma — a carta certa é a coisa
        # que mais importa.
        return candidates[0]


class OverridePrintLanguageResolver:
    """Consulta `card_print_override` (ADR 0012, Opção D) — a correspondência
    que um humano já confirmou manualmente para essa carta+idioma.

    Alimentada por `PrintOverrideRepository.set()`, chamada em
    `ScanService.confirm_result` sempre que a revisão confirma um print
    explícito para um idioma diferente de inglês. É exatamente a segunda
    implementação de `PrintLanguageResolver` que o ADR 0009 previu ("o dia em
    que existir uma base própria de traduções de print").
    """

    def resolve(
        self, session: Session, card_id: int, current_print: CardPrint | None, language: str
    ) -> CardPrint | None:
        if not language or language == "EN":
            return None
        return PrintOverrideRepository(session).get_print(card_id, language)


class CompositePrintLanguageResolver:
    """Encadeia resolvedores na ordem dada — o primeiro que achar, resolve.

    Ordem recomendada (ADR 0012): `OverridePrintLanguageResolver` antes de
    `SiblingPrintLanguageResolver` — uma correção aprendida do próprio uso
    sempre vence a heurística automática de "print irmão no catálogo
    sincronizado".
    """

    def __init__(self, resolvers: list[PrintLanguageResolver]) -> None:
        self._resolvers = resolvers

    def resolve(
        self, session: Session, card_id: int, current_print: CardPrint | None, language: str
    ) -> CardPrint | None:
        for resolver in self._resolvers:
            found = resolver.resolve(session, card_id, current_print, language)
            if found is not None:
                return found
        return None
