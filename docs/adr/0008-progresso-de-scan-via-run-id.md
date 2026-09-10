# ADR 0008 — Progresso de scan ao vivo usa `run_id` opaco, não `job_id`

**Status:** aceito
**Data:** 2026-09-10 (Fase 8)

## Contexto

O plano original (§9) previa `GET /api/v1/scans/{id}/events` (SSE) e
`DELETE /api/v1/scans/{id}` usando o mesmo `id` do histórico persistido
(`ScanJob.id`). Mas `ScanService._run()` só cria o `ScanJob` **depois** de
descobrir e filtrar as imagens por hash já visto (pode não sobrar trabalho
nenhum) — o `job_id` não existe no instante em que a requisição HTTP que
inicia o scan retorna. Expor isso de forma síncrona exigiria reordenar
esse fluxo (criar o job antes, sempre, mesmo sem imagens novas), tocando
código da Fase 5 já testado e em uso pela CLI, por um ganho pequeno.

## Decisão

`POST /api/v1/scans` inicia o scan numa thread e devolve um `run_id`
(UUID) imediatamente — antes de `ScanService.scan()` sequer começar.
`GET /api/v1/scans/live/{run_id}/events` (SSE) e
`DELETE /api/v1/scans/live/{run_id}` (cancela) usam esse token, mantido em
`web/scan_runner.py::ScanRunnerRegistry`, em memória, por processo. O
`job_id` de verdade chega dentro do evento final do stream
(`type="done"`) assim que `ScanService.scan()` retorna; a partir daí
`GET /api/v1/scans/{id}` (numérico, histórico persistido) funciona
exatamente como o plano original descrevia.

Cancelamento é cooperativo, também sem tocar `scan_service.py`: o callback
de progresso (já chamado depois de cada imagem, o mesmo gancho que
alimenta a barra do CLI) levanta uma exceção interna quando o run foi
marcado para cancelar; `ScanService.scan()` faz `rollback()` do lote ainda
não commitado (no máximo `COMMIT_BATCH_SIZE` imagens) e propaga — o que já
foi commitado fica.

## Consequências

- Positivo: zero mudança em `services/scan_service.py` — código da Fase 5,
  usado pela CLI, permanece intocado e continua coberto pelos mesmos
  testes.
- Positivo: cliente recebe feedback (um `run_id` para abrir o SSE)
  instantaneamente, sem esperar a descoberta de imagens terminar.
- Negativo: o registro de runs em `ScanRunnerRegistry` é em memória — não
  sobrevive a um restart do servidor. Aceito: um scan em andamento é, por
  natureza, efêmero; o histórico de verdade é o `ScanJob` persistido.
- Negativo: dois conceitos de "id" para scan (o `run_id` efêmero da
  execução ao vivo e o `job_id` persistido) — documentado aqui e no código
  (`scan_runner.py`) para não confundir quem for mexer depois.
