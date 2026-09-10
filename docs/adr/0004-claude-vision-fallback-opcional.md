# ADR 0004 — Claude Vision como fallback opcional, não OCR principal

**Status:** aceito, implementação adiada para a Fase 10
**Data:** 2026-09-08 (Fase 1)

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
comando do usuário** — não implementada ainda.

## Consequências

- Positivo: custo zero até o usuário decidir ligar; a ferramenta continua
  funcional e offline sem essa dependência.
- Positivo: quando implementado, o ganho é mensurável contra o mesmo
  corpus real da Fase 9 (ADR 0001) — "melhorou X% a um custo de Y por
  imagem" é uma frase com número, não uma promessa.
- Pendente: `ocr/claude_provider.py` não existe ainda; a "cascata" descrita
  no plano (§6.3) — tentar o próximo provider automaticamente — também não
  existe para nenhum provider, não só o Claude.
