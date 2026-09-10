"""ORM → dict, compartilhado entre as rotas JSON e HTML (nenhuma lógica aqui,
só achatamento — a mesma regra de `exporters/base.py::CollectionRow`)."""

from __future__ import annotations

from typing import Any

from ..db.tables import Card, CardPrint, CardSet, CollectionItem, ScanJob, ScanResult


def job_to_dict(job: ScanJob) -> dict[str, Any]:
    return {
        "id": job.id,
        "folder_path": job.folder_path,
        "status": job.status,
        "ocr_provider": job.ocr_provider,
        "workers": job.workers,
        "total_images": job.total_images,
        "processed": job.processed,
        "skipped": job.skipped,
        "auto_added": job.auto_added,
        "pending": job.pending,
        "failed": job.failed,
        "started_at": job.started_at.isoformat(),
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "error": job.error,
    }


def result_to_dict(result: ScanResult, *, with_image: bool = True) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": result.id,
        "scan_image_id": result.scan_image_id,
        "card_id": result.card_id,
        "card_print_id": result.card_print_id,
        "ocr_name_raw": result.ocr_name_raw,
        "ocr_code_raw": result.ocr_code_raw,
        "name_score": round(result.name_score, 4),
        "code_score": round(result.code_score, 4),
        "confidence": round(result.confidence, 4),
        "margin": round(result.margin, 4),
        "candidates": result.candidates or [],
        "decision": result.decision,
        "applied": result.applied,
        "collection_item_id": result.collection_item_id,
        "created_at": result.created_at.isoformat(),
        "decided_at": result.decided_at.isoformat() if result.decided_at else None,
    }
    if with_image:
        payload["image"] = {
            "id": result.scan_image.id,
            "file_path": result.scan_image.file_path,
            "status": result.scan_image.status,
            "error": result.scan_image.error,
        }
    return payload


def print_to_dict(print_row: CardPrint) -> dict[str, Any]:
    return {
        "id": print_row.id,
        "card_id": print_row.card_id,
        "set_code": print_row.set_code_full,
        "set_name": print_row.set_name,
        "set_prefix": print_row.set_prefix,
        "region": print_row.region,
        "rarity": print_row.rarity,
    }


def card_to_dict(card: Card, *, with_prints: bool = False) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": card.id,
        "name": card.name,
        "type": card.type,
        "attribute": card.attribute,
        "race": card.race,
        "archetype": card.archetype,
        "atk": card.atk,
        "def": card.defense,
        "level": card.level,
        "desc": card.desc,
    }
    if with_prints:
        payload["prints"] = [print_to_dict(p) for p in card.prints]
    return payload


def item_to_dict(item: CollectionItem) -> dict[str, Any]:
    return {
        "id": item.id,
        "card_id": item.card_id,
        "card_name": item.card.name,
        "card_print_id": item.card_print_id,
        "set_code": item.card_print.set_code_full if item.card_print else None,
        "rarity": item.card_print.rarity if item.card_print else None,
        "quantity": item.quantity,
        "condition": item.condition,
        "edition": item.edition,
        "language": item.language,
        "notes": item.notes,
        "source": item.source,
        "added_at": item.added_at.isoformat(),
    }


def set_to_dict(card_set: CardSet) -> dict[str, Any]:
    return {
        "set_code": card_set.set_code,
        "set_name": card_set.set_name,
        "num_of_cards": card_set.num_of_cards,
        "tcg_date": card_set.tcg_date.isoformat() if card_set.tcg_date else None,
    }
