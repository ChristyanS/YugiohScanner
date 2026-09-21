# Corpus de calibração de grade (deskew, ADR 0011)

3 digitalizações reais (fornecidas pelo usuário em 2026-09-18, páginas de
fichário 3x3 escaneadas em flatbed) — 27 cartas com gabarito de nome/set
code/passcode — mais 9 fotos do Scan #11 real do app (2026-09-20, job_id=11
no banco), revisado manualmente pelo usuário na tela `/review`: 92 cartas
reais confirmadas (nome/print resolvidos por `card_print_id`, gabarito lido
direto do catálogo) e 7 fundos de carta rejeitados manualmente (`card_back`,
ver abaixo). 2 das 11 fotos do Scan #11 foram descartadas do corpus por
serem bytes idênticos a `scan_05.png`/`scan_10.png` — a mesma página de
fichário foi digitalizada de novo entre as duas sessões. Ver a seção
"Deskew por célula" de `docs/adr/0011-grade-de-cartas-e-captura-wia.md`
para a medição completa (antes/depois do deskew por célula) e a seção
"Scan #11" para a calibragem contra dado real de uso, não só de teste.

## `expected.json`

Um array com uma entrada por foto:

```json
[
  {
    "file": "scan_01.png",
    "grid_size": "3x3",
    "cells": [
      {"name": "Ansatsu", "set_code": "SDY-016", "passcode": "48365709"}
    ]
  }
]
```

- `file`: nome do arquivo de imagem, relativo a esta pasta.
- `grid_size`: mesmo formato de `--grid-size` (`LINHASxCOLUNAS`).
- `source_image_id` (opcional, só nas fotos do Scan #11): `scan_image.id` de
  origem no banco real — só documentação/rastreabilidade, `calibrate_grid.py`
  ignora o campo.
- `cells`: uma entrada por célula, **em ordem de leitura** (linha a linha,
  esquerda→direita) — a mesma ordem que `manual_grid_cells` devolve.
  - `name`: nome **exato e em inglês** da carta (o nome canônico que o
    matching deveria resolver, verificado por passcode contra o banco —
    mesma convenção de `tests/fixtures/cards/expected.json`), não a
    tradução impressa na carta.
  - `set_code`: `null` quando a carta não mostra um set code (ex.: prints
    japoneses muito antigos sem essa linha impressa).
  - `passcode`: os dígitos do "Card ID" impresso, como string.
  - `card_back: true` **em vez de** `name`/`set_code`/`passcode`: célula é o
    verso de uma carta (slot vazio do fichário mostrando o fundo da carta
    seguinte, ou foto tirada com a carta virada) — não existe carta real
    pra conferir; `calibrate_grid.py` só verifica que o matching não
    autoconfirmou a célula como se fosse uma carta de verdade
    (`Decision.AUTO`). Achado real do Scan #11: `scan11_07.png`, 7 das 9
    células.

## Como usar

```bash
python scripts/calibrate_grid.py
python scripts/calibrate_grid.py --json
```

Roda a grade completa (deskew + ROIs + OCR + matching) uma vez por célula e
reporta acerto por campo. Precisa do catálogo real (`yugioh-scanner init`
antes) e do extra `cv` para o deskew ter efeito — sem `cv2`/`numpy`
instalados, o script ainda roda, mas mede o caminho de fallback (sem
deskew), não o caminho novo.
