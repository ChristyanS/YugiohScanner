# ADR 0001 — Limiares de confiança: julgamento de engenharia, calibração real feita (achado: gargalo é leitura, não limiar)

**Status:** aceito — limiares **mantidos**; corpus ampliado para 270 fotos reais
e um bug real de matching (colisão de nomes CJK degenerados) encontrado e
corrigido
**Data:** 2026-09-10; medição real em 2026-09-11; segunda medição (corpus
maior, bug corrigido) em 2026-09-13

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

## Segunda medição (2026-09-13) — corpus de 270 fotos reais, mão livre

O usuário forneceu 271 fotos reais tiradas à mão de cartas dentro de páginas
plásticas de fichário (`C:\...\Galeria Samsung\DCIM\cartas-yu-gi-oh`) — não é
o corpus de 20 fotos do `tests/fixtures/cards` (que continua intocado): este
corpus maior e mais variado (monstros Link/XYZ/Synchro/Fusão/Ritual, Magias,
cartas em EN/PT/JA/DE, várias cópias repetidas) ficou fora do repositório por
ser uma coleção pessoal — vive na própria pasta do usuário, referenciada por
`--corpus`, nunca copiada para o git.

### Gabarito sem gastar em API

O usuário não tem acesso à API paga da Anthropic, só ao Claude Code. Em vez do
provider `claude` (`ocr/claude_provider.py`, que exige `ANTHROPIC_API_KEY`),
o gabarito foi construído lendo as 271 fotos diretamente (visão do próprio
Claude Code, via 10 subagentes em paralelo, ~27 fotos cada) e reconciliando
cada leitura contra o catálogo local pelo mesmo `MatchingEngine` do app —
novo script `scripts/generate_ground_truth.py --readings <json>`, companheiro
de `calibrate_thresholds.py`. 266/271 resolveram automaticamente; 5 exigiram
investigação manual direta no banco (todas eram o mesmo padrão: prefixo de
set em itálico lido errado pelo transcritor, ex. `IT02`/`RA02` em vez de
`TT02` — confirmado consultando `card_print` pelo `card_id` certo). 1 carta
japonesa (`武神決戦`) não foi identificada nem manualmente e ficou de fora
(`unresolved.json`). Corpus final: **270 fotos com gabarito**.

### Bug real encontrado: colisão de nomes CJK degenerados

A varredura de limiares não achou **nenhuma** combinação com zero
falso-positivo — nem em `auto=0.99`. Investigando, duas fotos bateram, com
**100% e 85% de confiança**, em cartas completamente erradas. Causa raiz:
`domain/normalization.py::normalize_strict` mantém só `[a-z0-9 '-]` — um nome
alternativo (`CardAltName`) só em coreano normaliza para quase nada (o nome
KO de "H - Heated Heart" vira `"h-"`; o de "Arcana Force V - The Hierophant"
vira `"v-"`). Quando o RapidOCR lê mal uma carta japonesa e o texto
embaralhado deixa sobrar uma letra solta ("h", "v"), essa leitura degenerada
bate 100% contra o nome KO igualmente degenerado — carta errada, confiança
máxima. Reproduzido e confirmado consultando `CardAltName` diretamente para
as duas cartas.

**Correção** (`matching/candidates.py`): nomes — tanto o texto lido quanto os
candidatos do catálogo — cuja forma normalizada tenha menos de 4 caracteres
deixam de ser usados para casar por texto, nos tiers 0, 3 e 4 (`NameIndex`,
`_tier0_exact`, `_tier3_rerank`, e um corte cedo em `CandidateFinder.find`
para a leitura em si). A carta continua identificável pelo set code
(`_from_code_only`) — que é exatamente como as leituras JA deste corpus já
resolviam corretamente na maioria dos casos. Efeito colateral positivo:
casos que antes "ganhavam" um candidato de texto errado (e por isso nunca
caíam no caminho por código) agora caem certo em `_from_code_only` — a
correção não é só de segurança, também **subiu a taxa de acerto do nome**
(veja abaixo). Suíte completa (803 testes) roda limpa depois da mudança.

Achado secundário, não perigoso mas registrado: nomes alternativos em
japonês (`enrich-i18n`, ADR 0012) ainda têm tags `<ruby>...</ruby>` não
removidas no banco — não gera colisão porque o texto japonês real continua
presente dentro da tag, mas suja a normalização. Não corrigido nesta sessão
(exigiria reimportar ou migrar dados já sincronizados); registrado como
dívida técnica do importador `catalog_sources/yaml_yugi/`.

Também corrigido no caminho: `scripts/calibrate_thresholds.py` estava
quebrado desde o refactor de grade de cartas (ADR 0011) —
`ScanOutcome.read_name`/`.read_code`/`.status` migraram para `crops[0]` e o
script nunca foi atualizado. Sem essa correção a calibração nem rodava.

### Resultado (depois da correção)

270 fotos, RapidOCR (motor padrão), sem grade (`--grid` desligado):

| Métrica | Antes do fix | Depois do fix | Meta (§19) |
|---|---|---|---|
| Acerto do nome (top-1) | 72,6% | **77,8%** | ≥ 80% |
| Acerto do set code | 73,3% | 73,3% | ≥ 60% |

Corpus antigo (20 fotos, através de plástico de álbum, causa dominante não
resolvida): 40%. Este corpus (271 fotos, mão livre, fichário) está bem mais
perto da meta — a maioria das fotos está nítida e sem reflexo forte.

**A varredura de 0-falso-positivo continuou vazia mesmo depois do fix** — mas
por um motivo tranquilizador, não por outro bug: ela exige nome **e** set
code corretos ao mesmo tempo, e o set code (canto minúsculo) não valida em
boa parte das fotos mesmo quando o nome foi lido com 90-100% de confiança
(22 casos exatamente assim, todos com nome certo e código ausente). Isso não
é perigoso — o item entraria na coleção com a carta certa e print indefinido
(`card_print_id = NULL`, plano §4.3), não com a carta errada.

Conferido caso a caso: **nenhuma foto deste corpus levaria uma carta errada a
entrar sozinha na coleção sob os limiares atuais em produção** (`auto=0.93`).
O único quase-caso (`Dark Magician Girl the Dragon Knight` lido como `Dark
Magician Girl` — nome real e parecido de um card evoluído, mais um OCR que
truncou a leitura no meio) ficou em 92,3%, abaixo do limiar de auto-aceite —
foi corretamente para revisão manual. Não é um bug de normalização como os
dois de cima (as duas cartas são reais e o nome curto é de fato prefixo do
nome longo); fica registrado como risco residual conhecido, sem mudança de
código — um caso isolado não justifica reajustar a fórmula de margem (§7.4)
sem mais dados.

## Decisão (revisão 2026-09-13)

- `confidence_auto=0.93`/`confidence_review=0.70` **seguem sem mudança** — a
  varredura não achou combinação melhor (pelo motivo explicado acima, que é
  uma limitação do critério do script, não do matching), e a checagem caso a
  caso confirma zero carta errada auto-adicionada neste corpus, o maior e
  mais realista medido até agora.
- A correção de normalização (colisão CJK) e a de `calibrate_thresholds.py`
  ficam — bugs reais, independentes do resultado da calibração em si.
- Ação pendente, não decidida nesta sessão: revisar o `<ruby>` não removido
  do importador `yaml_yugi` (achado secundário acima).
- Próximos corpora já combinados com o usuário para uma futura calibração:
  fotos capturadas direto por scanner físico (ADR 0011, `scan-device`), e
  fotos de cartas soltas fora do plástico (o caso "fácil" que nenhum corpus
  testou até agora — nem este, nem o de 20 fotos).
