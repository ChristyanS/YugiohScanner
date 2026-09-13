# Proposta: fontes de dados para completar i18n/OCG do catálogo

**Status:** proposta para discussão (ainda não é ADR aceito)
**Data:** 2026-09-12
**Relacionado:** [`docs/proposta-i18n-cartas-e-sets.md`](proposta-i18n-cartas-e-sets.md),
[ADR 0009](adr/0009-idioma-do-print-por-regiao-existente.md),
[ADR 0010](adr/0010-cardset-como-produto-logico.md)

## Por que este documento existe

O objetivo do sistema é exigir o mínimo de intervenção humana possível ao
escanear cartas físicas. Isso depende do catálogo local ter, para toda
carta, nome/descrição no idioma físico da carta e o **print (set code)
regional correto** — sem isso, toda carta não-inglesa cai em revisão manual
ou fica com "print indefinido", o oposto do objetivo do produto.

`proposta-i18n-cartas-e-sets.md` e os ADRs 0009/0010 já investigaram isso a
fundo e concluíram, com dados reais da YGOPRODeck: "não existe código de set
regional para DE/FR/IT, PT só existe em produtos `OP*`, e OCG é uma lacuna
de fonte de dados sem solução no horizonte". Essa conclusão está correta
**para a YGOPRODeck** — mas, ao refazer a investigação ao vivo com outra
fonte de dados, encontrei algo que muda a decisão: **a lacuna é da fonte,
não da realidade**. Existem códigos de set regionais reais para
DE/FR/IT/PT/JA/KO; a YGOPRODeck simplesmente não os cadastra. Um projeto
comunitário já os tem, em volume, mantido ativamente.

Este documento **atualiza** (não substitui) as conclusões de 0009/0010 à
luz desse achado e responde às três hipóteses do usuário — trocar de base,
cruzar dados, ou gerar códigos por heurística — com pelo menos cinco opções
avaliadas, prós e contras de cada uma.

## Metodologia

Nada abaixo é suposição. Toda afirmação foi validada com chamadas reais
durante esta análise (12/09/2026): `curl` contra
`https://db.ygoprodeck.com/api/v7/` e download + inspeção do dataset
`DawnbrandBots/yaml-yugi` (branch `aggregate`, arquivo `cards.json`, ~94 MB,
gerado do Yugipedia). Os comandos estão citados para poderem ser
reexecutados.

---

## 1. O que foi verificado ao vivo nesta sessão

### 1.1 `cardsets.php` da YGOPRODeck não tem nenhum set OCG

```
GET https://db.ygoprodeck.com/api/v7/cardsets.php   → 1.035 sets hoje
grep "OCG" no resultado                              → 0 ocorrências
grep "Premium Pack 2026" no resultado                → 0 ocorrências
```

As páginas `ygoprodeck.com/pack/<nome>/ocg/` existem **no site**, mas não
vêm da API v7 que este projeto consome — é um pipeline de dados diferente,
não exposto publicamente. Confirma e reforça a conclusão de
`proposta-i18n-cartas-e-sets.md` §2.5: para este projeto, hoje, a YGOPRODeck
não é fonte de sets OCG, ponto final.

### 1.2 Caso real "Vanquish Soul Razen" (id 29302858) — já documentado como "sem tradução"

`proposta-i18n-cartas-e-sets.md` §1.1 confirmou, com a própria YGOPRODeck,
que essa carta **não tem nenhuma tradução cadastrada, em nenhum idioma**
(HTTP 400 para `pt`, `de`, `fr`, `ja`, mesmo por `id`).

No dataset `yaml-yugi`, a mesma carta (mesmo `password`/`id` = 29302858) tem:

```json
"name": {
  "en": "Vanquish Soul Razen", "de": "Bezwingerseele Razen",
  "es": "Derrota Almas Razen", "fr": "Âme du Vainqueur Razen",
  "it": "Sgomina Anima Razen", "pt": "Alma Aniquiladora Razen",
  "ja": "...", "ko": "...", "zh-TW": "...", "zh-CN": "..."
},
"sets": {
  "en": [{"set_number": "WISU-EN016", "set_name": "Wild Survivors", "rarities": ["Ultra Rare", "Collector's Rare"]}],
  "de": [{"set_number": "WISU-DE016", "set_name": "Wild Survivors", ...}],
  "fr": [{"set_number": "WISU-FR016", ...}],
  "it": [{"set_number": "WISU-IT016", ...}],
  "pt": [{"set_number": "WISU-PT016", ...}],
  "ja": [{"set_number": "DBWS-JP016", "set_name": "Deck Build Pack: Wild Survivors", "rarities": ["Super Rare"]}],
  "ko": [{"set_number": "DBWS-KR016", ...}]
}
```

Achado crítico: o print japonês não é "o mesmo set com letra trocada" — é um
**produto diferente** (`DBWS` vs `WISU`), com **raridade diferente** (Super
Rare vs Ultra Rare/Collector's Rare). Não existe transformação de string que
gere isso a partir do código inglês.

### 1.3 Caso "Dark Magician" (id 46986414) — contraprova em escala

```json
"set_lang_counts": {"en": 63, "de": 43, "fr": 40, "it": 41, "pt": 32, "ja": 44, "ko": 26}
```

Amostra alemã real: `SDY-G005`, `LOB-G003` (repare: sufixo **`-G`**, não
`-DE`). Amostra inglesa: `SYE-001`, `LOB-EN005`. **Nenhum desses 43 prints
alemães existe na YGOPRODeck** — ADR 0009 mediu, no catálogo local
sincronizado da YGOPRODeck, **0 prints em `DE`, `FR` ou `IT`** entre 44.517
prints totais.

Isso também mostra por que a hipótese de heurística (§1.5 abaixo) não
funcionaria mesmo se alguém tentasse "consertar": o sufixo de região nem é
consistente entre produtos e países (`-G` para Alemanha em `SDY`/`LOB`, não
`-DE`).

### 1.4 Estatística agregada do dataset inteiro (12.928 cartas, calculada agora)

| Idioma | Cartas com ≥1 print | Total de prints |
|---|---|---|
| EN | 12.568 | 34.738 |
| JA (OCG) | 12.860 | 26.865 |
| FR | 12.508 | 29.843 |
| DE | 12.499 | 30.451 |
| IT | 12.499 | 30.086 |
| KO | 12.387 | 21.540 |
| PT | 10.784 | 22.092 |
| zh-CN | 4.943 | 5.578 |
| zh-TW | 863 | 929 |

Comparar com a YGOPRODeck local (ADR 0009): **44.517 prints totais**, dos
quais 41.504 `EN`, 456 `PT`, **0** em `DE`/`FR`/`IT`, nenhum OCG. A diferença
de ordem de grandeza é o diagnóstico em si: a YGOPRODeck cobre bem um
idioma (inglês) e um produto de nicho (PT em `OP*`); o `yaml-yugi` cobre
prints reais em praticamente todo idioma para praticamente todo o catálogo,
incluindo OCG.

### 1.5 Isso invalida de vez a hipótese de heurística de string

`ADR 0009` já havia refutado "trocar `EN` por `PT` no código" com dados da
própria YGOPRODeck. Os achados desta sessão (§1.2 e §1.3) refutam de novo,
com evidência independente: o sufixo alemão é `-G`; o produto japonês tem
nome e raridade próprios. **Não existe `f(código_en) → código_regional`.**
Existe um *dado* por trás de cada região — e a pergunta certa não é "como
transformo o código" e sim "de onde vem esse dado".

---

## 2. As opções

### Opção A — Trocar a base (usar `yaml-yugi`/`YGOJSON` como fonte primária)

Substituir `YgoProDeckClient` por um cliente para o dataset agregado
(`yaml-yugi` diretamente, ou `YGOJSON`, que já funde YGOPRODeck + yaml-yugi +
Yugipedia num só pacote Python, MIT, regenerado diariamente).

- **Prós:** uma fonte só cobriria nome + set em 9+ idiomas, incluindo OCG;
  `YGOJSON` já resolve a fusão de fontes por conta própria; atualização
  diária automática; pacote Python instalável (`pip install ygojson`).
- **Contras:**
  - É **arquivo estático** (~94 MB o `cards.json` do yaml-yugi), não uma API
    de consulta pontual por HTTP — muda o modelo de sync de "paginação HTTP"
    (`cardinfo.php?num=N&offset=M`) para "baixar e parsear um dump inteiro".
    Isso não é necessariamente ruim (é até mais simples que paginação), mas
    é uma mudança de arquitetura no `SyncService`, não uma troca de URL.
  - Perde `card_images`, `card_prices`, e a política de cache de imagem já
    resolvida (ADR 0005) que hoje vem só da YGOPRODeck — precisaria manter
    as duas fontes de qualquer jeito (o que já é a Opção B).
  - Projeto mantido por poucas pessoas (bus factor: `iconmaster5326/YGOJSON`
    e `DawnbrandBots/yaml-yugi` são projetos de escala de hobby, não uma
    empresa).
  - **Nome oficial vs. fã-tradução — validado nesta sessão (ver §3.1).** O
    Yugipedia tem um mecanismo formal de sinalização (`is_translation_unofficial`),
    e ele funciona; mas o risco real medido é mais estreito e mais estranho
    do que a suspeita original: concentra-se em **nome inglês** de cartas
    ainda não lançadas em TCG (1,68% do catálogo) e, **sem sinalização
    nenhuma**, em **nome espanhol** — presente em 98% das cartas sem que
    exista um único print `es` correspondente no arquivo agregado. `de`/
    `fr`/`it`/`pt` não mostraram nenhuma ocorrência do flag na amostra
    completa. Detalhe completo e números em §3.1.

### Opção B — Cruzar dados (enriquecimento, não substituição)

Manter a YGOPRODeck como fonte primária (imagens, preços, API HTTP estável
e já testada em produção neste projeto) e adicionar uma etapa de sync que
importa `yaml-yugi`/`YGOJSON` só para preencher `CardAltName` e `CardPrint`
nos casos que a YGOPRODeck não cobre.

- **Prós:**
  - Não descarta nada que já funciona hoje.
  - Risco isolado: se `yaml-yugi` sumir ou mudar de formato, o catálogo
    primário (YGOPRODeck) continua íntegro — mesma filosofia "tudo ou nada"
    que `SyncService.sync()` já aplica (plano §16, "uma transação para
    tudo").
  - Encaixa no ponto de extensão que **já existe**: `SyncService.sync()` já
    roda em estágios sequenciais (sets → cards → alt_names por idioma,
    ver `src/yugioh_scanner/services/sync_service.py:170-209`); um estágio
    de enriquecimento a mais é aditivo, não uma reescrita.
  - `id`/`password` já são confirmados idênticos entre as duas fontes (é a
    mesma chave que a YGOPRODeck usa e que o `yaml-yugi` também usa como
    índice primário) — zero ambiguidade de join.
- **Contras:**
  - Duas fontes para conciliar; mais uma dependência de rede no sync
    (mitigável tratando-a como opcional/best-effort, como já é o padrão do
    projeto para `JA`/`KO` na proposta i18n).
  - Precisa de uma coluna de proveniência (`source` ou similar) em
    `CardPrint`/`CardAltName` para não confundir dado oficial da YGOPRODeck
    com dado agregado de terceiros — importante para auditoria e para nunca
    reportar um dado de fã como oficial.

### Opção C — Heurística de string (a hipótese original do usuário)

Gerar `EN001 → PT001` (ou `→ DE001`, `→ JP001`) por transformação de string
sobre o código já conhecido.

- **Prós:** nenhum, tecnicamente — zero custo de integração, mas produz
  dado errado com confiança.
- **Contras:** refutada duas vezes com dados reais: ADR 0009 (região `E`
  não é idioma, PT só existe de fato em produtos `OP*`) e, nesta sessão,
  com evidência independente (§1.2: sufixo alemão é `-G`, não `-DE`; §1.3:
  produto OCG tem nome e raridade próprios, não é cópia com letra trocada).
  Incluída aqui só para fechar a pergunta do usuário com evidência, não
  como opção viável.

### Opção D — Tabela de overrides alimentada pelo próprio uso

Sem tocar fontes externas: quando o usuário resolve manualmente um "print
indefinido" na tela `/review`, gravar essa correspondência numa tabela
própria (algo como `card_print_override`), reaproveitada em scans futuros
da mesma carta/idioma.

- **Prós:**
  - Zero dependência externa, zero risco de dado errado de terceiro, zero
    custo de manutenção de sync.
  - O sistema já tem o ponto de extensão certo: `PrintLanguageResolver`
    (`src/yugioh_scanner/matching/print_language.py`) é um `Protocol`, e o
    próprio ADR 0009 já previu explicitamente "o dia em que existir uma
    base própria de traduções de print, uma segunda implementação
    (ex.: `DatabaseTranslationPrintLanguageResolver`) troca por injeção".
- **Contras:** não pré-carrega nada — só aprende depois que o usuário já
  passou pela dor manual uma vez por carta. Não ajuda quem está começando a
  digitalizar uma coleção grande do zero (o caso atual do usuário).

### Opção E — Híbrida (recomendada)

**B + D.** Enriquecimento em lote via `yaml-yugi`/`YGOJSON` cobre a maioria
dos casos *antes* de qualquer scan acontecer (resolve o problema para quem
está começando do zero); a tabela de override (D) funciona como rede de
segurança para o que nem a fonte agregada tiver, ou para correção pontual
do usuário. As duas já usam o mesmo ponto de extensão
(`PrintLanguageResolver`), então não competem entre si — D é consultado
como fallback de última linha depois que B já tiver sido tentado.

---

## 3. Riscos a validar antes de confiar nos dados

### 3.1 Nome oficial vs. fã-tradução — validado nesta sessão

A hipótese ("Yugipedia pode misturar tradução de fã com nome oficial sem
distinção") foi checada indo direto ao código-fonte do pipeline `yaml-yugi`
(não só no dataset publicado), com um resultado mais específico — e em
parte mais tranquilizador — do que a suspeita original.

1. **O mecanismo de sinalização existe e chega até o dado publicado.**
   Yugipedia tem templates de wiki `{{Unofficial name}}` /
   `{{Unofficial lore}}` para marcar tradução não-oficial. O scraper do
   `yaml-yugi` (`src/common.py::set_unofficial_translation_flag`) converte
   isso num campo estruturado `is_translation_unofficial: {name: {<idioma>:
   true}, text: {<idioma>: true}}`. Confirmado em `src/job_ocgtcg.py:107-108`
   que esse campo é copiado para o documento final de cada carta, e em
   `src/merge.js` (13 linhas — só concatena os JSONs individuais, sem
   remover campos) que ele sobrevive até o `aggregate/cards.json` publicado.
2. **Medido no dataset inteiro (12.928 cartas):** só 217 (1,68%) têm alguma
   sinalização, e 167 delas são em **nome inglês** — sempre o mesmo padrão:
   carta ainda não lançada em TCG, cujo "nome em inglês" no Yugipedia é uma
   tradução de trabalho do japonês (exemplos reais encontrados: "Fiendsmith
   Requiem", "Missing Burroughs, the Dark Ruler of the Highest Heaven",
   "Synchro Creed"). **Nenhuma ocorrência do flag foi encontrada em `de`,
   `fr`, `it` ou `pt`** na amostra completa.
3. **Achado novo, fora da hipótese original: o espanhol é o caso realmente
   arriscado, por um motivo diferente.** 98% das cartas (12.705/12.928) têm
   `name.es` preenchido — mas **nenhuma carta no arquivo agregado tem print
   em `es`** (`sets.es` ausente em 100% dos casos, medido no dataset
   inteiro). E esse campo **nunca** é sinalizado por
   `is_translation_unofficial` (0 ocorrências para `es`) — a convenção do
   wiki parece reservada a "ainda não lançado em inglês", não a "nunca
   lançado fisicamente neste idioma".
4. **Achado colateral: defasagem entre o dado por-carta e o arquivo
   consolidado.** Fui direto ao arquivo por-carta do `yaml-yugi` na branch
   `master` (`data/cards/29302858.json`, não a branch `aggregate` publicada)
   para "Vanquish Soul Razen" e ele **tem** prints espanhóis reais e
   genuínos: `WISU-SP016`, `MP24-SP139`, `RA05-SP134`. Esse dado **não**
   está no `aggregate/cards.json` que baixei duas vezes nesta sessão para
   essa mesma carta. Ou seja, além da questão oficial-vs-fã, existe uma
   defasagem de atualização entre a branch `master` (por carta) e a branch
   `aggregate` (consolidada) — uma integração via arquivo agregado herdaria
   esse atraso sem perceber.

**Conclusão prática:**
- `name.<idioma>` segue sendo tratável como exibição/sugestão, nunca como
  critério único de auto-match — mas o risco concreto identificado é
  específico: nome inglês de cartas não lançadas em TCG (detectável via
  `is_translation_unofficial` ou `limit_regulation.tcg == "Not yet
  released"`) e nome espanhol de forma geral (quase sempre presente, nunca
  sinalizado, sem garantia de produto físico).
- `sets.<idioma>` (o dado que importa de verdade para este projeto — o
  print/set code) parece confiável **quando presente** — não encontrei
  nenhum caso de set code fabricado. Mas "ausente no `aggregate` não
  significa ausente na fonte": a Opção B/E, se implementada, deveria
  considerar consultar o dado por-carta (`master/data/cards/<password>.json`)
  como complemento, ou ao menos monitorar a defasagem entre as duas
  branches antes de tratar o agregado como completo.

### 3.2 Outros riscos

1. **Tamanho e cadência do dump.** ~94 MB, regenerado diariamente (branch
   `aggregate`), mas com defasagem observada em relação à branch `master`
   (§3.1.4). Decidir frequência de re-sync (não precisa ser a cada
   `yugioh-scanner sync`; pode ser um comando separado, ex.
   `db enrich-i18n`, análogo a `db probe-languages`) e se vale a pena
   consultar `master` para os casos que o agregado não cobrir.
2. **Licença.** Dados são compilação de texto de jogo (© Studio
   Dice/SHUEISHA, TV TOKYO, Konami) — mesma situação de fato que já existe
   hoje com a YGOPRODeck (uso pessoal, não comercial). O código gerador do
   `yaml-yugi` é AGPL-3.0, o que não afeta o uso somente-leitura dos dados
   publicados. Não é um bloqueador novo, mas vale registrar.
3. **Bus factor.** Projeto de escala de hobby. Mitigação: tratar como fonte
   **opcional** — se o enriquecimento falhar ou o formato mudar, o sync
   principal (YGOPRODeck) continua funcionando normalmente e o dado antigo
   de enriquecimento persiste no banco.

---

## 4. Como isso destrava as Fases 9, 10 e 11

- **Fase 9 (calibração):** hoje, boa parte das cartas não-inglesas cai em
  "sem print definido"/"manual" por **falta de dado**, não por falha de
  reconhecimento — isso contamina a medição de precisão do pipeline de OCR
  com um problema que não é de OCR. Com mais prints reais catalogados, a
  calibração passa a medir o que deveria: taxa de acerto do reconhecimento
  sobre um catálogo realista.
- **Fase 10 (fallback Claude Vision):** o LLM já é capaz de ler set code e
  idioma da foto (plano §6.4); sem os prints regionais no banco, a Camada 3
  de validação (`domain/setcode.py`, plano §7.2 — "o candidato só vira set
  code se existir no catálogo") descarta um código lido **corretamente**
  só porque a fonte de dados não o tem. O enriquecimento aumenta o
  aproveitamento real do fallback caro, sem enfraquecer a garantia de nunca
  aceitar código não validado.
- **Fase 11 (documentação):** a decisão tomada a partir deste documento
  vira um ADR novo, ao lado de 0009/0010.

---

## 5. Próximos passos (fora do escopo deste documento)

Este documento não implementa nada. Depois da sua decisão sobre
A / B / D / E:
1. Escrever o ADR correspondente (padrão do repo: proposta → decisão → ADR).
2. Só então desenhar a implementação: módulo de fonte adicional (paralelo a
   `ygoprodeck/`), nova etapa no `SyncService.sync()`, migração de schema
   para a coluna de proveniência e/ou a tabela de override, e validação do
   risco §3.1 (nome oficial vs. fã-tradução) antes de qualquer auto-match
   usar nome de fonte agregada.
