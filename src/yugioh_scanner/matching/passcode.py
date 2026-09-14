"""Resolução de passcode contra o catálogo — a camada 3 do plano de identidade
por Card ID (mesmo espírito de `matching/resolver.py` para set code).

A regra que nunca se quebra, igual à do set code: **na dúvida, `None`**. Um
passcode lido que não bate com nenhuma carta real do catálogo não identifica
nada — cai para o caminho de nome/set code, exatamente como hoje.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.tables import Card
from ..domain.passcode import clean_passcode


def resolve_passcode(session: Session, raw: str | None) -> int | None:
    """`Card.id` se o passcode lido existir de verdade no catálogo.

    Testa a leitura já corrigida (`domain.passcode.clean_passcode` resolve
    confusões comuns de OCR letra↔dígito) contra o banco — só aceito quando
    bate com uma carta real, nunca inventado.
    """
    cleaned = clean_passcode(raw)
    if cleaned is None:
        return None

    card_id = int(cleaned)
    return session.execute(select(Card.id).where(Card.id == card_id)).scalar_one_or_none()
