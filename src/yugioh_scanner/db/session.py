"""Fábrica de sessões e escopo transacional.

Regra da arquitetura (plano §2.3): **um único escritor**. Workers de OCR nunca
recebem uma sessão; eles devolvem DTOs e quem escreve é o processo principal.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(
        bind=engine,
        autoflush=False,
        expire_on_commit=False,  # objetos continuam utilizáveis após o commit
        future=True,
    )


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    """Transação com commit no sucesso e rollback em qualquer exceção."""
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


class Database:
    """Agrupa engine + fábrica de sessões.

    É o objeto que os serviços recebem por injeção — nenhum deles cria engine
    por conta própria, o que mantém a configuração em um lugar só.
    """

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.session_factory = make_session_factory(engine)

    @contextmanager
    def session(self) -> Iterator[Session]:
        with session_scope(self.session_factory) as session:
            yield session

    def dispose(self) -> None:
        self.engine.dispose()
