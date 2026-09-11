# ADR 0001 — Limiares de confiança: julgamento de engenharia, calibração real feita (achado: gargalo é leitura, não limiar)

**Status:** aceito — limiares **mantidos**, com achado concreto documentado
**Data:** 2026-09-10; medição real em 2026-09-11

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

## Decisão original (2026-09-10)

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

## Medição real (2026-09-11)

O usuário forneceu 20 fotos reais (`imagens-test/`, copiadas para
`tests/fixtures/cards/`) — cartas em português (a maioria), duas em inglês,
uma em japonês, fotografadas dentro de um álbum/binder de plástico (não
soltas sobre fundo liso, que é o caso de uso documentado em
`images/preprocess.py`). `scripts/calibrate_thresholds.py` rodou o pipeline
real (RapidOCR) contra o catálogo completo (14.533 cartas, com nomes
alternativos FR/DE/IT/PT sincronizados para esta medição).

**Resultado bruto: 40% de acerto top-1 do nome** (meta: ≥80%) — bem abaixo.
Nenhuma combinação de `auto`/`review` no *sweep* consegue 0 falso-positivo
com recall acima de 25%: **não é um problema de limiar.** Ajustar
`confidence_auto`/`review` decide só o quão conservador é aceitar o que *já*
foi lido certo — quando a leitura em si está errada na maioria dos casos,
nenhum limiar resolve isso.

**Duas causas raiz identificadas, investigando foto a foto:**

1. **Bug real, corrigido nesta sessão:** `OCRResult.joined()` concatenava as
   caixas de texto "na ordem em que o motor as devolveu" — o RapidOCR não
   garante ordem de leitura (esquerda→direita) entre caixas, e várias fotos
   mostravam o nome saindo embaralhado (`"UDIMA C OREDADO DEPVEPSO IR"` em
   vez de `"AHRIMA O SOBERADO PERVERSO"`). `TextLine` ganhou um campo `x`
   (posição horizontal, preenchido por `rapidocr_provider.py` e
   `tesseract_provider.py`) e `joined()` agora ordena por ele antes de
   concatenar. Ganho real, mas pequeno (35% → 40%) — a causa dominante é a
   próxima.
2. **Não corrigido, fora do escopo desta sessão:** a transcrição
   caractere-a-caractere do RapidOCR está genuinamente ruim nessas fotos
   específicas (`"UDIMA"` em vez de `"AHRIMA"`, `"MA NIOUU D"` em vez de
   `"ALMA ANIQUILADORA"` etc. — nenhum reordenamento de caixa conserta isso).
   A hipótese mais provável, a partir da inspeção visual dos recortes: as
   fotos foram tiradas **através do plástico do álbum** (reflexo, leve
   desfoque, compressão), não com a carta solta sobre fundo liso — a
   condição que `detect_card_bounds` (plano §5.2) foi desenhada para lidar
   bem. Não investigado a fundo nem "consertado às cegas": mudar a
   heurística de detecção de borda é uma mudança de risco real em código já
   testado (Fase 3), sem confiança suficiente de que ajudaria sem medir de
   novo — exatamente o tipo de mudança que pede aval explícito antes.

**Achado tranquilizador, apesar do número baixo:** em nenhuma combinação de
limiar testada um card errado teria entrado **sozinho** na coleção — mesmo
as leituras piores (`"D/D/D Contract Change"` como leitura errada repetida
para 3 fotos diferentes) ficaram com confiança ~65-75%, abaixo de qualquer
limiar de auto-aceite razoável. A política de confiança (§7.5) está fazendo
exatamente o que devia: quando a leitura é ruim, manda para revisão manual
em vez de arriscar. O problema medido é de **taxa de acerto do OCR nessas
fotos**, não de segurança da política.

## Decisão (revisão 2026-09-11)

- `confidence_auto`/`confidence_review` **não mudam** — o *sweep* não
  encontrou um limiar melhor que o atual para este corpus (a causa não é o
  limiar, ver acima).
- A correção de ordenação (#1) fica — é uma correção de bug real,
  independente do resultado da calibração.
- **Próximo passo recomendado, não executado:** pedir ao usuário 3-5 fotos
  das mesmas cartas (ou outras) **fora do álbum**, sobre uma superfície lisa
  — é o teste mais barato para confirmar ou descartar a hipótese do
  plástico/reflexo antes de investir em melhorar a detecção de borda para
  fotos de álbum.

## Consequências

- Positivo: a lacuna de acurácia é real, medida e tem causa investigada —
  não é mais "não sabemos", é "sabemos que a leitura carácter-a-carácter
  falha nessas condições específicas, e por quê provavelmente".
- Positivo: confirmado que a política de confiança não deixaria passar um
  erro sozinho neste corpus, mesmo com a taxa de leitura baixa — a rede de
  segurança (revisão manual) funciona.
- Positivo: um bug real de ordenação foi encontrado e corrigido por causa
  desta medição — o tipo de coisa que só aparece com foto de verdade, nunca
  com o corpus sintético.
- Negativo: a ferramenta ainda não está calibrada para o caso de uso real
  que este corpus representa (cartas em álbum) — só para o caso documentado
  (carta solta, fundo liso), que nenhuma das 20 fotos testou.
- Ação pendente: fotos fora do álbum, para isolar a causa; ou, se o usuário
  confirmar que fotografar em álbum é o uso real esperado, um investimento
  deliberado (fora desta sessão) em detecção de borda mais robusta a
  reflexo/textura.
