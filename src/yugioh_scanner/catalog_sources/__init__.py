"""Fontes de dados de catálogo além da YGOPRODeck (ADR 0012).

Cada subpacote é um fornecedor de **enriquecimento**, nunca de identidade: só
`ygoprodeck/` cria `Card` novas. Um fornecedor aqui só preenche
`CardAltName`/`CardPrint`/`CardSet` para cartas que já existem, e é sempre
opcional — uma falha nunca derruba o sync principal
(docs/proposta-fontes-dados-catalogo.md).
"""

from __future__ import annotations
