# ADR 0001 — Limiares de confiança: julgamento de engenharia, pendente de calibração real

**Status:** aceito, revisão pendente (Fase 9)
**Data:** 2026-09-10

## Contexto

`domain/confidence.py` decide se uma leitura de OCR entra sozinha na coleção
(`AUTO`), pede confirmação (`PENDING`) ou vai para revisão manual (`MANUAL`).
Os valores atuais (`confidence_auto=0.93`, `confidence_review=0.70`,
`agreement_auto=0.88`, `min_margin=0.08`, `ambiguous_margin=0.05`) foram
escolhidos nas Fases 1–4 por julgamento de engenharia — nenhum foi medido
contra fotos reais, porque nenhuma existia ainda no repositório.

O plano (§19.4, §9) sempre tratou isso como um estado temporário e
explicitamente adiado: os limiares "de palpite" viram medição assim que
houver um corpus real (15–25 fotos, variadas, fornecidas pelo usuário — não
geradas nem baixadas, porque sintético não testa a coisa real: ruído de
câmera, ângulo, brilho de sleeve/foil).

## Decisão

1. Manter os valores atuais como default até a calibração acontecer —
   mudá-los sem medição seria trocar um palpite por outro.
2. Construir a ferramenta de calibração (`scripts/calibrate_thresholds.py`)
   e o teste de aceitação (`tests/ocr/test_corpus_accuracy.py`) **agora**,
   mesmo sem o corpus — para que a calibração seja rodar um comando, não
   escrever código, no dia em que as fotos chegarem.
3. `tests/ocr/test_corpus_accuracy.py` pula (não falha) enquanto
   `tests/fixtures/cards/expected.json` não existir — sinaliza o estado
   pendente sem quebrar a suíte.
4. Quando o corpus existir: rodar `scripts/calibrate_thresholds.py`, revisar
   a tabela de limiares com 0 falso-positivo em auto-aceite, atualizar
   `confidence_auto`/`confidence_review` em `config.py` com o valor medido, e
   **atualizar este ADR** (não criar um novo) com os números e a data.

## Consequências

- Positivo: nenhum limiar "calibrado" falso é documentado como se fosse
  medido — a lacuna fica visível (aqui, no README do corpus, e no teste que
  pula) em vez de escondida atrás de um número que parece autoritativo.
- Positivo: o trabalho de Fase 9 que não depende de fotos reais (a
  ferramenta de calibração, os benchmarks de performance do §20.5, o gate de
  cobertura) não fica bloqueado — só a calibração de fato fica.
- Negativo: até a calibração real acontecer, a política de decisão continua
  sem garantia medida de que `AUTO` é seguro o bastante para fotos reais —
  o corpus sintético (`test_real_ocr.py`) prova que o *pipeline* funciona,
  não que os *limiares* estão certos.
- Ação pendente: quando o usuário fornecer o corpus, rodar a calibração e
  fechar este ADR com os números reais.
