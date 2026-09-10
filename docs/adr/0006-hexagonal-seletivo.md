# ADR 0006 — Hexagonal aplicado só onde há troca real de implementação

**Status:** aceito
**Data:** 2026-09-08 (Fase 1)

## Contexto

Arquitetura hexagonal (portas e adaptadores) tem custo real: uma interface
abstrata, uma implementação concreta, e a indireção entre as duas. Vale a
pena quando existe (ou vai existir de verdade) mais de um adaptador atrás
da porta. Aplicá-la em todo lugar "por princípio" — repositórios atrás de
uma interface `Repository` abstrata, por exemplo — adiciona uma camada que
nunca vai ganhar um segundo implementador neste projeto.

## Decisão

`Protocol` (porta) + múltiplas implementações concretas (adaptadores) só
em dois lugares:

- **OCR** (`ocr/base.py::OCRProvider`) — 5 motores previstos no plano,
  trocáveis por configuração (`OCR_PROVIDER`).
- **Exportação** (`exporters/base.py::ExportProfile`) — 6 perfis
  registrados por `(formato, nome)`, cada um uma classe nova sem tocar
  `services/export_service.py` nem a CLI.

Em todo o resto — `repositories/`, `services/` — as classes são concretas,
com SQLAlchemy dentro, sem interface abstrata. Não há um segundo banco
planejado (SQLite é definitivo para este projeto, não um placeholder até
"crescer" para Postgres).

## Consequências

- Positivo: adicionar um provider de OCR ou um perfil de exportação é uma
  classe nova + uma entrada no registry — zero mudança arquitetural, como
  demonstrado na prática (Fase 7: 4 perfis de exportação adicionados sem
  tocar `services/`; Fase 9: descoberto que `paddle`/`easyocr` faltam,
  adicioná-los quando chegar a vez segue o mesmo molde).
- Positivo: repositórios concretos são mais fáceis de navegar — sem pular
  entre uma interface e sua única implementação.
- Risco aceito: se um dia o projeto precisar trocar de banco, essa
  reescrita não é gratuita. Julgado improvável o bastante para não pagar o
  custo antecipadamente (*interface sem segundo implementador é
  overengineering*).
