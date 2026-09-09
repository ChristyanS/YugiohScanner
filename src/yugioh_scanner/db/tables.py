"""Schema do banco (plano §4).

Modelos SQLAlchemy 2.0 declarativos e tipados. Este módulo descreve a *forma*
dos dados; regra de negócio mora em `domain/` e `services/`.

Decisões que valem relembrar aqui:

* `Card.id` é o passcode do YGOPRODeck — chave natural e estável.
* `CardPrint.id` é surrogate porque a API **não fornece** ID de print (§0.3.2),
  e a unicidade precisa incluir a raridade: a mesma carta pode aparecer duas
  vezes no mesmo set em raridades diferentes (§0.3.3).
* `CollectionItem.card_print_id` é NULL quando o set não foi identificado —
  isso é um estado esperado, não um erro.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, ClassVar

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import JSON


def utcnow() -> dt.datetime:
    """UTC ingênuo (sem tzinfo).

    SQLite não guarda offset; misturar datetimes aware e naive é fonte clássica
    de comparações erradas. Padronizamos em naive-UTC em todo o banco.
    """
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    """Base declarativa com mapeamento de JSON explícito."""

    type_annotation_map: ClassVar[dict[Any, Any]] = {dict[str, Any]: JSON, list[Any]: JSON}


# --------------------------------------------------------- vocabulários fixos
# Guardados como texto com CHECK: SQLite não tem ENUM nativo e migrar um ENUM
# é sempre pior do que migrar um CHECK.

CONDITIONS = ("Near Mint", "Lightly Played", "Moderately Played", "Heavily Played", "Damaged")
EDITIONS = ("1st Edition", "Unlimited", "Limited")
COLLECTION_SOURCES = ("scan", "manual", "import")
SCAN_JOB_STATUSES = ("running", "done", "failed", "cancelled")
SCAN_IMAGE_STATUSES = ("pending", "ok", "ocr_empty", "invalid", "error")
SCAN_DECISIONS = ("auto", "pending", "confirmed", "rejected", "unmatched")

DEFAULT_CONDITION = "Near Mint"
DEFAULT_EDITION = "Unlimited"
DEFAULT_LANGUAGE = "EN"


def _check_in(column: str, allowed: tuple[str, ...], name: str) -> CheckConstraint:
    values = ", ".join(f"'{v}'" for v in allowed)
    return CheckConstraint(f"{column} IN ({values})", name=name)


# ------------------------------------------------------------------- catálogo


class Card(Base):
    """Uma carta do jogo, independente de em quais sets ela foi impressa."""

    __tablename__ = "card"

    #: Passcode do YGOPRODeck (chave natural — não geramos surrogate).
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    #: Nome passado por `normalize_strict` — é por aqui que a busca acontece.
    name_normalized: Mapped[str] = mapped_column(String(255), nullable=False)

    type: Mapped[str] = mapped_column(String(64), nullable=False)
    frame_type: Mapped[str | None] = mapped_column(String(64))
    human_readable_type: Mapped[str | None] = mapped_column(String(128))
    desc: Mapped[str] = mapped_column(Text, nullable=False, default="")

    atk: Mapped[int | None] = mapped_column(Integer)
    defense: Mapped[int | None] = mapped_column("def", Integer)  # `def` é keyword
    level: Mapped[int | None] = mapped_column(Integer)
    attribute: Mapped[str | None] = mapped_column(String(16))
    race: Mapped[str | None] = mapped_column(String(64))
    archetype: Mapped[str | None] = mapped_column(String(128))
    scale: Mapped[int | None] = mapped_column(Integer)
    linkval: Mapped[int | None] = mapped_column(Integer)

    typeline: Mapped[list[Any] | None] = mapped_column(JSON)
    linkmarkers: Mapped[list[Any] | None] = mapped_column(JSON)

    tcg_date: Mapped[dt.date | None] = mapped_column(Date)
    ocg_date: Mapped[dt.date | None] = mapped_column(Date)
    konami_id: Mapped[int | None] = mapped_column(Integer)
    has_effect: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    ygoprodeck_url: Mapped[str | None] = mapped_column(String(512))

    synced_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    images: Mapped[list[CardImage]] = relationship(
        back_populates="card", cascade="all, delete-orphan", lazy="selectin"
    )
    prints: Mapped[list[CardPrint]] = relationship(
        back_populates="card", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_card_name_normalized", "name_normalized"),
        Index("ix_card_archetype", "archetype"),
    )

    def __repr__(self) -> str:
        return f"<Card {self.id} {self.name!r}>"


class CardImage(Base):
    """Uma arte de uma carta.

    Limitação conhecida da API (§0.3.4): quando uma carta tem várias artes, nada
    diz qual print usa qual. Guardamos todas e marcamos a principal.
    """

    __tablename__ = "card_image"

    #: ID da arte no YGOPRODeck (igual ao passcode na arte principal).
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    card_id: Mapped[int] = mapped_column(ForeignKey("card.id", ondelete="CASCADE"), nullable=False)

    image_url: Mapped[str] = mapped_column(String(512), nullable=False)
    image_url_small: Mapped[str | None] = mapped_column(String(512))
    image_url_cropped: Mapped[str | None] = mapped_column(String(512))

    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: Preenchido quando o arquivo já foi baixado para o cache local (§14).
    cached_path: Mapped[str | None] = mapped_column(String(512))

    card: Mapped[Card] = relationship(back_populates="images")

    __table_args__ = (Index("ix_card_image_card", "card_id"),)


class CardSet(Base):
    """Um set/edição, vindo de `cardsets.php`.

    Atenção ao duplo sentido de `set_code` na API (§0.3.1): aqui ele é o
    **prefixo** (`LOB`, `MP24`), enquanto em `CardPrint.set_code_full` é o
    código completo do print (`LOB-EN001`).
    """

    __tablename__ = "card_set"

    set_code: Mapped[str] = mapped_column(String(32), primary_key=True)
    set_name: Mapped[str] = mapped_column(String(255), nullable=False)
    num_of_cards: Mapped[int | None] = mapped_column(Integer)
    tcg_date: Mapped[dt.date | None] = mapped_column(Date)
    set_image: Mapped[str | None] = mapped_column(String(512))
    cached_image_path: Mapped[str | None] = mapped_column(String(512))
    synced_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    prints: Mapped[list[CardPrint]] = relationship(back_populates="card_set")

    __table_args__ = (Index("ix_card_set_name", "set_name"),)

    def __repr__(self) -> str:
        return f"<CardSet {self.set_code} {self.set_name!r}>"


class CardPrint(Base):
    """Uma impressão específica de uma carta em um set.

    Chave lógica: (card_id, set_code_full, rarity). A raridade entra porque a
    mesma carta aparece duas vezes no mesmo set em raridades diferentes.
    """

    __tablename__ = "card_print"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    card_id: Mapped[int] = mapped_column(ForeignKey("card.id", ondelete="CASCADE"), nullable=False)

    #: Código completo como a API entrega: "LOB-EN001", "SDK-001", "MP24-EN001".
    set_code_full: Mapped[str] = mapped_column(String(64), nullable=False)
    #: Forma canônica usada na busca (maiúsculas, sem separadores exóticos).
    set_code_normalized: Mapped[str] = mapped_column(String(64), nullable=False)

    #: FK opcional: quando o prefixo não casa com nenhum set conhecido,
    #: preservamos o print com set_prefix NULL em vez de descartá-lo.
    set_prefix: Mapped[str | None] = mapped_column(
        ForeignKey("card_set.set_code", ondelete="SET NULL")
    )
    set_name: Mapped[str] = mapped_column(String(255), nullable=False)

    rarity: Mapped[str | None] = mapped_column(String(64))
    rarity_code: Mapped[str | None] = mapped_column(String(16))
    region: Mapped[str | None] = mapped_column(String(4))
    number: Mapped[str | None] = mapped_column(String(8))

    #: Snapshot informativo (§0.3.6) — nunca fonte de verdade para preço.
    set_price: Mapped[float | None] = mapped_column(Float)
    synced_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    card: Mapped[Card] = relationship(back_populates="prints")
    card_set: Mapped[CardSet | None] = relationship(back_populates="prints")

    __table_args__ = (
        Index(
            "ux_print",
            "card_id",
            "set_code_full",
            text("COALESCE(rarity, '')"),
            unique=True,
        ),
        Index("ix_print_code_normalized", "set_code_normalized"),
        Index("ix_print_card", "card_id"),
        Index("ix_print_prefix", "set_prefix"),
    )

    def __repr__(self) -> str:
        return f"<CardPrint {self.set_code_full} ({self.rarity})>"


# -------------------------------------------------------------------- coleção


class CollectionItem(Base):
    """Cópias que você possui de uma combinação (carta, print, condição, ...).

    `card_print_id` NULL significa "sei qual carta é, não sei qual print" — o
    caso que o briefing pede explicitamente que continue funcionando.
    """

    __tablename__ = "collection_item"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    card_id: Mapped[int] = mapped_column(ForeignKey("card.id", ondelete="CASCADE"), nullable=False)
    card_print_id: Mapped[int | None] = mapped_column(
        ForeignKey("card_print.id", ondelete="SET NULL")
    )

    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    condition: Mapped[str] = mapped_column(String(32), nullable=False, default=DEFAULT_CONDITION)
    edition: Mapped[str] = mapped_column(String(32), nullable=False, default=DEFAULT_EDITION)
    language: Mapped[str] = mapped_column(String(8), nullable=False, default=DEFAULT_LANGUAGE)
    notes: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="manual")

    added_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, onupdate=utcnow
    )

    card: Mapped[Card] = relationship(lazy="joined")
    card_print: Mapped[CardPrint | None] = relationship(lazy="joined")

    __table_args__ = (
        # SQLite trata NULLs como distintos em UNIQUE, então um print NULL
        # furaria a unicidade. O COALESCE resolve (plano §4.3).
        Index(
            "ux_collection",
            "card_id",
            text("COALESCE(card_print_id, -1)"),
            "condition",
            "edition",
            "language",
            unique=True,
        ),
        Index("ix_collection_card", "card_id"),
        Index("ix_collection_print", "card_print_id"),
        # Quantidade zero significa "não tenho": a linha é removida, não zerada.
        CheckConstraint("quantity > 0", name="ck_collection_quantity_positive"),
        _check_in("condition", CONDITIONS, "ck_collection_condition"),
        _check_in("edition", EDITIONS, "ck_collection_edition"),
        _check_in("source", COLLECTION_SOURCES, "ck_collection_source"),
    )

    def __repr__(self) -> str:
        return f"<CollectionItem card={self.card_id} print={self.card_print_id} x{self.quantity}>"


# --------------------------------------------------------------------- scans


class ScanJob(Base):
    """Uma execução do scanner sobre uma pasta."""

    __tablename__ = "scan_job"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    folder_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    ocr_provider: Mapped[str] = mapped_column(String(32), nullable=False)
    workers: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running")

    total_images: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    processed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    skipped: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    auto_added: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pending: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    started_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime)
    error: Mapped[str | None] = mapped_column(Text)

    images: Mapped[list[ScanImage]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )

    __table_args__ = (_check_in("status", SCAN_JOB_STATUSES, "ck_scan_job_status"),)

    def __repr__(self) -> str:
        return f"<ScanJob {self.id} {self.status} {self.processed}/{self.total_images}>"


class ScanImage(Base):
    """Uma foto processada.

    A identidade é o **hash do conteúdo**, com índice único global — não o
    caminho. É isso que faz `scan` ser idempotente mesmo se você renomear ou
    mover os arquivos (plano §13.1).
    """

    __tablename__ = "scan_image"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(
        ForeignKey("scan_job.id", ondelete="CASCADE"), nullable=False
    )

    file_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    file_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    file_size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    error: Mapped[str | None] = mapped_column(Text)
    ocr_raw: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    ocr_ms: Mapped[int | None] = mapped_column(Integer)

    created_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    job: Mapped[ScanJob] = relationship(back_populates="images")
    results: Mapped[list[ScanResult]] = relationship(
        back_populates="scan_image", cascade="all, delete-orphan"
    )

    __table_args__ = (
        # Global, não por job: rodar de novo em outra pasta também deduplica.
        Index("ux_scan_image_hash", "file_hash", unique=True),
        Index("ix_scan_image_job", "job_id"),
        _check_in("status", SCAN_IMAGE_STATUSES, "ck_scan_image_status"),
    )

    def __repr__(self) -> str:
        return f"<ScanImage {self.id} {self.status} {self.file_path}>"


class ScanResult(Base):
    """O que o matching concluiu sobre uma imagem, e o que foi feito com isso.

    É tabela separada (e não colunas dentro de `ScanImage`) de propósito: o dia
    em que uma foto puder conter várias cartas, isto vira 1—N sem migração de
    dados (plano §22).
    """

    __tablename__ = "scan_result"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scan_image_id: Mapped[int] = mapped_column(
        ForeignKey("scan_image.id", ondelete="CASCADE"), nullable=False
    )

    card_id: Mapped[int | None] = mapped_column(ForeignKey("card.id", ondelete="SET NULL"))
    card_print_id: Mapped[int | None] = mapped_column(
        ForeignKey("card_print.id", ondelete="SET NULL")
    )

    ocr_name_raw: Mapped[str | None] = mapped_column(String(512))
    ocr_code_raw: Mapped[str | None] = mapped_column(String(64))

    name_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    code_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    #: Distância para o 2º candidato. Tão importante quanto o score (§7.4).
    margin: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    #: Top-5 com scores, para a tela de revisão não precisar re-rodar o matching.
    candidates: Mapped[list[Any] | None] = mapped_column(JSON)

    decision: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    #: True quando esta imagem já contribuiu +1 na coleção. Garante que uma
    #: foto conte no máximo uma vez, mesmo com --reprocess (plano §13.2).
    applied: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    collection_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("collection_item.id", ondelete="SET NULL")
    )

    created_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    decided_at: Mapped[dt.datetime | None] = mapped_column(DateTime)

    scan_image: Mapped[ScanImage] = relationship(back_populates="results")

    __table_args__ = (
        Index("ix_scan_result_image", "scan_image_id"),
        Index("ix_scan_result_card", "card_id"),
        # Índice parcial: a fila de revisão é a consulta quente desta tabela.
        Index(
            "ix_scan_result_pending",
            "decision",
            sqlite_where=text("decision = 'pending'"),
        ),
        _check_in("decision", SCAN_DECISIONS, "ck_scan_result_decision"),
    )

    def __repr__(self) -> str:
        return f"<ScanResult {self.id} {self.decision} conf={self.confidence:.2f}>"


# ---------------------------------------------------------------------- meta


class SyncState(Base):
    """Chave-valor com o estado da sincronização e do schema."""

    __tablename__ = "sync_state"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, onupdate=utcnow
    )

    def __repr__(self) -> str:
        return f"<SyncState {self.key}={self.value!r}>"


#: Chaves conhecidas de `sync_state`.
SYNC_KEY_DATABASE_VERSION = "ygoprodeck_database_version"
SYNC_KEY_DATABASE_DATE = "ygoprodeck_database_date"
SYNC_KEY_LAST_FULL_SYNC = "last_full_sync"
SYNC_KEY_CARD_COUNT = "card_count"
SYNC_KEY_PRINT_COUNT = "print_count"
SYNC_KEY_SET_COUNT = "set_count"
