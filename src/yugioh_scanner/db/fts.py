"""Índice de busca textual FTS5 (plano §4.3 e §7.3).

`card_fts` é uma tabela FTS5 de *conteúdo externo*: ela não duplica os dados,
apenas indexa `card.name_normalized`, e triggers a mantêm em sincronia.

Segurança: FTS5 tem sintaxe própria de consulta (`NEAR`, `OR`, `"..."`, `-`).
Passar texto de OCR cru para `MATCH` é uma injeção real — pode alterar a
semântica da busca ou causar erro de sintaxe. Por isso todo termo passa por
`sanitize_fts_query` (plano §21).
"""

from __future__ import annotations

import re

from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

#: A DDL (tabela virtual + triggers) mora na migração `0001_initial_schema`,
#: não aqui: migração é história congelada e não pode mudar quando este módulo
#: mudar. Este arquivo tem apenas o que roda em tempo de execução.
FTS_TABLE = "card_fts"

#: Tudo que não for letra/dígito/espaço é descartado antes de chegar ao MATCH.
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def sanitize_fts_query(raw: str, *, prefix_last: bool = True, operator: str = "OR") -> str:
    """Converte texto arbitrário em uma consulta FTS5 segura.

    Extrai apenas tokens alfanuméricos e os une com o operador escolhido;
    opcionalmente transforma o último em busca por prefixo (``dragon*``), o que
    ajuda quando o OCR corta o fim da palavra.

    O padrão é ``OR`` porque a entrada vem de OCR. Com ``AND``, **um único
    caractere errado em qualquer palavra zera o resultado** — medido no catálogo
    real: 5 de 6 leituras típicas ("BLUE-EYES WH1TE DRAGON", "P0T OF GREED"…)
    devolviam zero candidatos. Com ``OR`` todas devolvem 50, ordenados por bm25,
    e o rerank por similaridade escolhe entre eles.

    Devolve string vazia quando não sobra nenhum token — o chamador deve tratar
    isso como "sem candidatos" em vez de executar um MATCH vazio.

    >>> sanitize_fts_query('Blue-Eyes White Dragon')
    'blue OR eyes OR white OR dragon*'
    >>> sanitize_fts_query('Dark Magician', operator='AND')
    'dark AND magician*'
    >>> sanitize_fts_query('" OR name MATCH "x')
    'or OR name OR match OR x'
    """
    tokens = _TOKEN_RE.findall(raw.lower())
    if not tokens:
        return ""
    if prefix_last and len(tokens[-1]) >= 2:
        tokens[-1] = f"{tokens[-1]}*"
    return f" {operator} ".join(tokens)


def search_card_ids(session: Session, query: str, *, limit: int = 50) -> list[int]:
    """IDs de cartas que casam com o texto, ordenados por relevância (bm25).

    Retorna lista vazia se a consulta sanitizada ficar vazia.
    """
    safe = sanitize_fts_query(query)
    if not safe:
        return []
    rows = session.execute(
        text(
            f"SELECT rowid FROM {FTS_TABLE} "
            f"WHERE {FTS_TABLE} MATCH :q ORDER BY bm25({FTS_TABLE}) LIMIT :limit"
        ),
        {"q": safe, "limit": limit},
    ).scalars()
    return list(rows)


#: Índice sobre `card_alt_name` (nomes em FR/DE/IT/PT — plano §7.1
#: multilíngue). Tabela separada de `card_fts`: uma carta pode ter várias
#: linhas (uma por idioma), o que uma FTS5 de conteúdo externo não permite.
ALT_FTS_TABLE = "card_alt_fts"


def search_alt_card_ids(session: Session, query: str, *, limit: int = 50) -> list[int]:
    """IDs de carta (não rowid — `card_alt_fts` guarda `card_id` à parte,
    porque seu rowid é o de `card_alt_name`, não o da carta) que casam com o
    texto via nome alternativo, ordenados por relevância (bm25).
    """
    safe = sanitize_fts_query(query)
    if not safe:
        return []
    rows = session.execute(
        text(
            f"SELECT card_id FROM {ALT_FTS_TABLE} "
            f"WHERE {ALT_FTS_TABLE} MATCH :q ORDER BY bm25({ALT_FTS_TABLE}) LIMIT :limit"
        ),
        {"q": safe, "limit": limit},
    ).scalars()
    return list(rows)


def rebuild_fts(engine: Engine) -> None:
    """Reconstrói o índice a partir da tabela `card`.

    Necessário depois de uma carga em massa feita com os triggers desabilitados,
    ou para consertar um índice que ficou fora de sincronia.
    """
    with engine.begin() as conn:
        conn.execute(text(f"INSERT INTO {FTS_TABLE}({FTS_TABLE}) VALUES('rebuild')"))


def fts_row_count(engine: Engine) -> int:
    """Quantidade de linhas indexadas (para diagnóstico em `db status`)."""
    with engine.connect() as conn:
        result = conn.execute(text(f"SELECT count(*) FROM {FTS_TABLE}")).scalar()
    return int(result or 0)
