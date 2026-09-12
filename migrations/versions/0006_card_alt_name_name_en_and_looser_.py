"""card alt name: name_en + check de idioma por formato

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-12 15:00:00.000000

Duas mudanças, motivadas pela investigação registrada em
docs/proposta-i18n-cartas-e-sets.md:

1. `name_en`: a resposta de `cardinfo.php?language=...` traz de graça o nome
   em inglês (`name_en`) junto do nome traduzido. Guardado só como conferência
   de integridade — se um dia divergir de `card.name` para o mesmo `card_id`,
   é sinal de tradução mal vinculada na fonte, não bug nosso. NULL em bancos
   sincronizados antes desta coluna existir, até o próximo `sync`.

2. O CHECK de `language` era uma enumeração fechada (`'FR','DE','IT','PT'`).
   Confirmado ao vivo contra a API (2026-09-12) que `ja` e `ko` também são
   aceitos, apesar de não documentados nem no guia oficial nem na própria
   mensagem de erro que a API devolve para um valor inválido — ou seja, a
   lista "oficial" já estava desatualizada. Trocado por uma checagem de
   *formato* (2-3 letras maiúsculas) em vez de enumeração: evita que o
   próximo idioma "surpresa" exija outra migração. A lista de idiomas que o
   app efetivamente baixa/exibe por padrão continua vivendo em Python
   (`db.tables.ALT_NAME_LANGUAGES`), que não precisa de migração para mudar.

Tabela recriada à mão (não `batch_alter_table`) pelo mesmo motivo da 0002:
`card_alt_name` tem uma tabela-sombra FTS5 (`card_alt_fts`, criada em 0003)
com triggers amarrados ao nome da tabela — mexer nisso via reflexão
automática do Alembic é risco desnecessário quando dá para escrever à mão.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LEGACY_LANGUAGES = "'FR', 'DE', 'IT', 'PT'"


def _drop_fts_triggers() -> None:
    op.execute("DROP TRIGGER IF EXISTS card_alt_fts_au")
    op.execute("DROP TRIGGER IF EXISTS card_alt_fts_ad")
    op.execute("DROP TRIGGER IF EXISTS card_alt_fts_ai")


def _create_fts_triggers() -> None:
    op.execute(
        """
        CREATE TRIGGER card_alt_fts_ai AFTER INSERT ON card_alt_name BEGIN
            INSERT INTO card_alt_fts(rowid, name_normalized, card_id)
            VALUES (new.id, new.name_normalized, new.card_id);
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER card_alt_fts_ad AFTER DELETE ON card_alt_name BEGIN
            DELETE FROM card_alt_fts WHERE rowid = old.id;
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER card_alt_fts_au AFTER UPDATE ON card_alt_name BEGIN
            UPDATE card_alt_fts SET name_normalized = new.name_normalized, card_id = new.card_id
            WHERE rowid = old.id;
        END
        """
    )
    op.execute("CREATE UNIQUE INDEX ux_card_alt_name ON card_alt_name (card_id, language)")
    op.execute("CREATE INDEX ix_card_alt_name_normalized ON card_alt_name (name_normalized)")


def upgrade() -> None:
    _drop_fts_triggers()
    op.execute("DROP INDEX IF EXISTS ux_card_alt_name")
    op.execute("DROP INDEX IF EXISTS ix_card_alt_name_normalized")

    op.execute("ALTER TABLE card_alt_name RENAME TO card_alt_name_old")
    op.execute(
        """
        CREATE TABLE card_alt_name (
            id INTEGER NOT NULL,
            card_id INTEGER NOT NULL,
            language VARCHAR(8) NOT NULL,
            name VARCHAR(255) NOT NULL,
            name_normalized VARCHAR(255) NOT NULL,
            synced_at DATETIME NOT NULL,
            desc TEXT NOT NULL DEFAULT '',
            name_en VARCHAR(255),
            PRIMARY KEY (id),
            FOREIGN KEY(card_id) REFERENCES card (id) ON DELETE CASCADE,
            CONSTRAINT ck_card_alt_name_language_format
                CHECK (length(language) BETWEEN 2 AND 3 AND language = upper(language))
        )
        """
    )
    op.execute(
        """
        INSERT INTO card_alt_name (
            id, card_id, language, name, name_normalized, synced_at, desc
        )
        SELECT id, card_id, language, name, name_normalized, synced_at, desc
        FROM card_alt_name_old
        """
    )
    op.execute("DROP TABLE card_alt_name_old")

    _create_fts_triggers()


def downgrade() -> None:
    # Reverter para o CHECK antigo perderia silenciosamente linhas com
    # language='JA'/'KO' (ou qualquer coisa fora de FR/DE/IT/PT) que ele
    # rejeitaria — mesma cautela da 0002. Falha alto e explícito em vez de
    # descartar dado do usuário calado.
    connection = op.get_bind()
    has_unsupported = connection.execute(
        text(f"SELECT 1 FROM card_alt_name WHERE language NOT IN ({_LEGACY_LANGUAGES}) LIMIT 1")
    ).first()
    if has_unsupported is not None:
        raise RuntimeError(
            "downgrade indisponível: existem card_alt_name.language fora de "
            f"({_LEGACY_LANGUAGES}) que o CHECK antigo (antes da 0006) rejeitaria."
        )

    _drop_fts_triggers()
    op.execute("DROP INDEX IF EXISTS ux_card_alt_name")
    op.execute("DROP INDEX IF EXISTS ix_card_alt_name_normalized")

    op.execute("ALTER TABLE card_alt_name RENAME TO card_alt_name_old")
    op.execute(
        f"""
        CREATE TABLE card_alt_name (
            id INTEGER NOT NULL,
            card_id INTEGER NOT NULL,
            language VARCHAR(8) NOT NULL,
            name VARCHAR(255) NOT NULL,
            name_normalized VARCHAR(255) NOT NULL,
            synced_at DATETIME NOT NULL,
            desc TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (id),
            FOREIGN KEY(card_id) REFERENCES card (id) ON DELETE CASCADE,
            CONSTRAINT ck_card_alt_name_language CHECK (language IN ({_LEGACY_LANGUAGES}))
        )
        """
    )
    op.execute(
        """
        INSERT INTO card_alt_name (
            id, card_id, language, name, name_normalized, synced_at, desc
        )
        SELECT id, card_id, language, name, name_normalized, synced_at, desc
        FROM card_alt_name_old
        """
    )
    op.execute("DROP TABLE card_alt_name_old")

    _create_fts_triggers()
