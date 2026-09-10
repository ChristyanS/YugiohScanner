"""Comandos `yugioh-scanner collection …` (plano §8 e §11.4)."""

from __future__ import annotations

import typer

from ..db.tables import CollectionItem
from ..errors import AmbiguousCardError
from .context import collection_service
from .errors import handle_errors
from .render import fail, print_json, print_key_values, print_table, success, warn

app = typer.Typer(help="Gerenciar a coleção manualmente.", no_args_is_help=True)


def _row(item: CollectionItem) -> list[object]:
    set_label = item.card_print.set_code_full if item.card_print else "(indefinido)"
    rarity = item.card_print.rarity if item.card_print and item.card_print.rarity else ""
    return [item.id, item.card.name, set_label, rarity, item.quantity, item.condition]


def _item_dict(item: CollectionItem) -> dict[str, object]:
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
    }


@app.command("list")
@handle_errors
def list_cmd(
    search: str = typer.Option(None, "--search", help="Filtra por nome (parcial)."),
    set_code: str = typer.Option(
        None, "--set", help="Filtra pelo prefixo do set (ex.: LOB, MP24)."
    ),
    no_set: bool = typer.Option(False, "--no-set", help="Só itens sem set identificado."),
    sort: str = typer.Option("name", "--sort", help="Coluna: name, quantity, added ou set."),
    descending: bool = typer.Option(False, "--desc", help="Ordem decrescente."),
    limit: int = typer.Option(None, "--limit", "-n", help="Limita o número de linhas."),
    as_json: bool = typer.Option(False, "--json", help="Saída em JSON."),
) -> None:
    """Lista e filtra a coleção."""
    with collection_service() as service:
        items = service.list_items(
            search=search,
            set_prefix=set_code,
            no_set=no_set,
            sort=sort,
            descending=descending,
            limit=limit,
        )
        if as_json:
            print_json([_item_dict(item) for item in items])
            return
        print_table(
            "Coleção",
            ["ID", "Carta", "Set", "Raridade", "Qtd", "Condição"],
            [_row(item) for item in items],
            empty_message="Coleção vazia (ou nenhum item bate com o filtro).",
        )


@app.command("add")
@handle_errors
def add_cmd(
    name: str = typer.Argument(..., help="Nome da carta (aceita parcial)."),
    set_code: str = typer.Option(None, "--set-code", help="Código do print, se souber."),
    quantity: int = typer.Option(1, "--qty", "-q", min=1, help="Quantas cópias."),
    condition: str = typer.Option("Near Mint", "--condition", help="Estado de conservação."),
    edition: str = typer.Option(
        "Unlimited", "--edition", help="1st Edition, Unlimited ou Limited."
    ),
    language: str = typer.Option("EN", "--language", help="Código do idioma (EN, PT, ...)."),
    notes: str = typer.Option(None, "--notes", help="Anotação livre."),
    as_json: bool = typer.Option(False, "--json", help="Saída em JSON."),
) -> None:
    """Adiciona cópias pelo nome — resolve parcial e pergunta se for ambíguo.

    Critério de aceitação do plano §6: `collection add "blue eyes"` encontra
    a carta mesmo sem o nome completo, e pede para escolher quando o nome
    corresponder a mais de uma carta com confiança parecida.
    """
    with collection_service() as service:
        try:
            item = service.add_manual(
                name,
                set_code=set_code,
                quantity=quantity,
                condition=condition,
                edition=edition,
                language=language,
                notes=notes,
            )
        except AmbiguousCardError as exc:
            chosen = _disambiguate(name, exc.candidates)
            if chosen is None:
                fail("Cancelado.")
                raise typer.Exit(code=1) from None
            item = service.add_manual(
                chosen,
                set_code=set_code,
                quantity=quantity,
                condition=condition,
                edition=edition,
                language=language,
                notes=notes,
            )

        if as_json:
            print_json(_item_dict(item))
        else:
            success(f"{item.card.name}: {item.quantity}x ({item.condition})")


def _disambiguate(query: str, candidates: list[str]) -> str | None:
    """Pede ao usuário para escolher entre nomes parecidos.

    Devolve `None` se o usuário cancelar (Ctrl+C ou opção inválida repetida).
    """
    warn(f"'{query}' corresponde a mais de uma carta:")
    for index, candidate in enumerate(candidates, start=1):
        typer.echo(f"  {index}. {candidate}")
    choice = typer.prompt(
        "Escolha o número (ou Enter para cancelar)", default="", show_default=False
    )
    if not choice.strip():
        return None
    try:
        position = int(choice.strip())
    except ValueError:
        return None
    if not 1 <= position <= len(candidates):
        return None
    return candidates[position - 1]


@app.command("remove")
@handle_errors
def remove_cmd(
    item_id: int = typer.Argument(..., help="ID do item (veja em `collection list`)."),
    quantity: int = typer.Option(None, "--qty", "-q", help="Quantas remover (padrão: todas)."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Não perguntar."),
    as_json: bool = typer.Option(False, "--json", help="Saída em JSON."),
) -> None:
    """Remove cópias de um item. Sem `--qty`, remove todas (apaga a linha)."""
    with collection_service() as service:
        item = service.get_item(item_id)
        card_name = item.card.name
        before = item.quantity

        if not yes:
            target = "todas as cópias" if quantity is None else f"{quantity} cópia(s)"
            typer.confirm(f"Remover {target} de '{card_name}' (tem {before})?", abort=True)

        remaining = service.remove(item_id, quantity)

    if as_json:
        print_json({"id": item_id, "card_name": card_name, "remaining": remaining})
    else:
        success(f"{card_name}: {before} → {remaining}" + (" (removido)" if remaining == 0 else ""))


@app.command("set-qty")
@handle_errors
def set_qty_cmd(
    item_id: int = typer.Argument(..., help="ID do item."),
    quantity: int = typer.Argument(..., help="Nova quantidade absoluta (0 remove)."),
    as_json: bool = typer.Option(False, "--json", help="Saída em JSON."),
) -> None:
    """Define a quantidade exata de um item."""
    with collection_service() as service:
        item = service.get_item(item_id)
        card_name = item.card.name
        remaining = service.set_quantity(item_id, quantity)

    if as_json:
        print_json({"id": item_id, "card_name": card_name, "quantity": remaining})
    else:
        success(f"{card_name}: quantidade agora é {remaining}")


@app.command("set-print")
@handle_errors
def set_print_cmd(
    item_id: int = typer.Argument(..., help="ID do item."),
    set_code: str = typer.Argument(..., help="Código do print (ex.: LOB-001)."),
    as_json: bool = typer.Option(False, "--json", help="Saída em JSON."),
) -> None:
    """Resolve manualmente o set de um item que estava indefinido."""
    with collection_service() as service:
        item = service.resolve_print_by_code(item_id, set_code)
        result = _item_dict(item)

    if as_json:
        print_json(result)
    else:
        success(f"{item.card.name}: set definido como {result['set_code']}")


@app.command("show")
@handle_errors
def show_cmd(
    item_id: int = typer.Argument(..., help="ID do item."),
    as_json: bool = typer.Option(False, "--json", help="Saída em JSON."),
) -> None:
    """Mostra o detalhe de um item: carta, print e todos os prints existentes."""
    with collection_service() as service:
        item = service.get_item(item_id)
        all_prints = service.repo.prints_for_card(item.card_id)

        if as_json:
            print_json(
                {
                    **_item_dict(item),
                    "card": {
                        "name": item.card.name,
                        "type": item.card.type,
                        "attribute": item.card.attribute,
                        "atk": item.card.atk,
                        "def": item.card.defense,
                        "level": item.card.level,
                        "archetype": item.card.archetype,
                    },
                    "known_prints": [
                        {"set_code": p.set_code_full, "rarity": p.rarity, "set_name": p.set_name}
                        for p in all_prints
                    ],
                }
            )
            return

        print_key_values(
            item.card.name,
            {
                "tipo": item.card.type,
                "atk/def": f"{item.card.atk}/{item.card.defense}"
                if item.card.atk is not None
                else "",
                "arquétipo": item.card.archetype or "",
                "quantidade": item.quantity,
                "condição": item.condition,
                "edição": item.edition,
                "idioma": item.language,
                "set desta cópia": item.card_print.set_code_full
                if item.card_print
                else "indefinido",
                "notas": item.notes or "",
            },
        )
        print_table(
            "Prints conhecidos desta carta",
            ["Set Code", "Raridade", "Set"],
            [[p.set_code_full, p.rarity or "", p.set_name] for p in all_prints],
        )


@app.command("stats")
@handle_errors
def stats_cmd(
    as_json: bool = typer.Option(False, "--json", help="Saída em JSON."),
) -> None:
    """Números gerais da coleção (o mesmo que o dashboard mostra)."""
    with collection_service() as service:
        stats = service.stats()

    if as_json:
        print_json(stats.as_dict())
    else:
        print_key_values(
            "Coleção",
            {
                "cartas diferentes": stats.distinct_cards,
                "cópias totais": stats.total_copies,
                "sem set identificado": stats.items_without_print,
            },
        )
