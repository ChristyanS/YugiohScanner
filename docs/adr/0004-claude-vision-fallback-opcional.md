# ADR 0004 — Claude Vision como fallback opcional, não OCR principal

**Status:** aceito e implementado (Fase 10)
**Data:** 2026-09-08 (Fase 1); atualizado 2026-09-12 (Fase 10)

## Contexto

OCR local (RapidOCR/Tesseract) erra em condições difíceis: sleeve
reflexivo, ângulo ruim, holografia. Um modelo de visão como o Claude lê
essas fotos muito melhor, mas cada chamada tem custo em dinheiro e depende
de rede — inaceitável como caminho *padrão* de uma ferramenta que hoje é
100% offline depois do sync inicial.

## Decisão

Claude Vision entra só como **fallback opcional, desligado por padrão**
(`OCR_FALLBACK_PROVIDER=none`). Quando ligado (`=claude`), reprocessa
apenas as leituras que ficaram abaixo do limiar de confiança — não
substitui o motor local, complementa. Detalhes de implementação (§6.4 do
plano): `claude-opus-5` como padrão configurável, structured outputs
(schema `{name, set_code, rarity, edition, language, legible}`) em vez de
regex sobre prosa, Message Batches API para lotes grandes (metade do
custo), imagem recortada/reduzida antes de enviar (não a original). Sem
`ANTHROPIC_API_KEY`, o provider não é registrado e a leitura degrada em
silêncio para revisão manual — nunca uma falha ruidosa.

Isto é a Fase 10 do roadmap, marcada explicitamente como **opcional, sob
comando do usuário** — implementada em 2026-09-12.

## Implementação (Fase 10)

`ocr/claude_provider.py` traz `ClaudeVisionOCRProvider`, registrado como
`claude` em `ocr/registry.py` (extra `pip install -e ".[llm]"`). A cascata
do plano §6.3 vive em `ScanService._try_fallback`/`_get_fallback_provider`
(`services/scan_service.py`): depois que o OCR local decide, **só** as
leituras que não saíram `auto` são reprocessadas — a imagem já preparada
(recortada/reduzida a 1600px, nunca a original) é reenviada como região
`full`, o novo texto passa pelo mesmo `MatchingEngine`, e o resultado só
substitui o original se a confiança **não piorar**. `--fallback-provider`
na CLI e `YGS_OCR_FALLBACK_PROVIDER` sobrescrevem o padrão `none`.

Estrutura da resposta: `output_config.format` (JSON Schema) força o schema
`{name, set_code, rarity, edition, language, legible}` — sem regex sobre
prosa, como o plano pedia. Sem `ANTHROPIC_API_KEY`, `warmup()` levanta
`LlmApiKeyMissingError`; `ScanService` captura isso (e qualquer
`YugiohScannerError`) uma única vez por execução e desliga a cascata em
silêncio pelo resto do scan — nunca uma falha ruidosa, como o ADR original
exigia.

**Desvios conscientes em relação ao plano §6.4, documentados e não
bloqueadores:**

- **Sem Message Batches API.** A cascata roda uma chamada síncrona por
  imagem duvidosa, no processo principal, não em lote assíncrono. Como só
  ~15% das imagens chegam a essa etapa, o ganho de custo do Batches (50%,
  mas assíncrono e sem streaming de progresso) não compensava a
  complexidade adicional nesta primeira versão. `Settings.llm_concurrency`
  já existe para uma futura versão paralela/em lote, mas não é usado ainda.
- **Sem cascata paralela.** Cada chamada de fallback é sequencial dentro do
  laço de resultados do scan — aceitável porque o volume é pequeno e a
  chamada já é I/O-bound (não bloqueia CPU de outros workers), mas significa
  que um scan com muitas imagens difíceis fica mais lento, não mais rápido,
  enquanto a cascata roda.
- **Ganho não medido contra o corpus da Fase 9.** O ADR original pedia
  "ganho medido no mesmo corpus, com custo por imagem reportado" antes de
  considerar a Fase 10 fechada. Isso ainda não foi feito — depende de rodar
  a cascata contra o corpus real de `tests/fixtures/cards/` com chave de
  API válida, o que é uma ação do usuário, não do código.

## Consequências

- Positivo: custo zero até o usuário decidir ligar; a ferramenta continua
  funcional e offline sem essa dependência (`ocr_fallback_provider=none`
  nunca importa `anthropic`).
- Positivo: quando o usuário rodar a cascata contra fotos reais, o ganho é
  mensurável contra o mesmo corpus real da Fase 9 (ADR 0001) — "melhorou X%
  a um custo de Y por imagem" é uma frase com número, não uma promessa.
- Pendente: a medição em si (ver desvios acima) e uma eventual versão em
  lote/paralela se o volume de imagens difíceis justificar.
