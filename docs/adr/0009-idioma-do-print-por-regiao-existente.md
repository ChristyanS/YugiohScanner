# ADR 0009 — Idioma do print: procurar irmão existente, não converter código

**Status:** aceito
**Data:** 2026-09-12

## Contexto

O usuário tem cartas físicas em PT-BR, alemão e inglês, e queria que a
revisão de scan (e, quando possível, o processamento automático) apontasse
para o print (set number) correspondente ao idioma da carta física — a
hipótese inicial era que `EN → PT`/`EN → DE` no set code é uma troca de
região na string (`LOB-EN001` → `LOB-PT001`).

Antes de desenhar a solução, essa hipótese foi checada contra o catálogo
real sincronizado (44.517 prints, YGOPRODeck):

| Região | Prints  |
|--------|---------|
| `EN`   | 41.504  |
| `None` | 1.825   |
| `E`    | 732     |
| `PT`   | 456     |

Nenhum print tem região `DE`, `FR` ou `IT`. Confirmado ao vivo contra a API
(`cardinfo.php?language=de` para o Mago Negro): o nome e a descrição vêm
traduzidos, mas `card_sets[]` continua listando os mesmos códigos `-EN`.
Cartas físicas alemãs/francesas/italianas usam **o mesmo código de set** que
a versão inglesa — não existe "LOB-DE001" para converter.

`PT` só aparece em produtos `OP*` (OTS Tournament Pack, `OP07`–`OP19`) — o
único produto TCG com português como idioma oficial de impressão, desde
~2020. Exemplo real: `OP13-EN006` e `OP13-PT006` são duas linhas de
`card_print` para a mesma carta, mesmo número, mesma raridade (checado em
25 pares EN/PT do catálogo real: 0 divergência de raridade). `E` (2002-2004,
Metal Raiders/Magic Ruler etc.) é um marcador histórico de "relançamento
europeu multi-idioma", não corresponde a um idioma específico.

## Decisão

"Idioma do print" não é uma transformação de string sobre o código — é uma
**consulta por print irmão já existente**: mesma carta, mesmo `set_prefix`,
mesmo `number`, `region` diferente. Implementado em
`matching/print_language.py` como `PrintLanguageResolver` (`Protocol`) +
`SiblingPrintLanguageResolver` (implementação de hoje, baseada só no que o
catálogo sincronizado já tem).

Quando a consulta não encontra nada (o caso comum — DE/FR/IT, ou PT fora de
produtos `OP`), a resposta correta é "nada para converter": o print já
identificado continua sendo o certo, e só `CollectionItem.language` precisa
registrar o idioma da carta física (campo que já existia, sem CHECK).

O idioma em si é detectado durante o matching por nome: quando o texto lido
casa com uma linha de `card_alt_name` (FR/DE/IT/PT), esse idioma vira
`NameCandidate.language` → `MatchResult.matched_language` →
`ScanResult.detected_language` — pré-selecionado na tela de revisão, com
correção manual sempre disponível (`review.html`, seletor de idioma ao lado
do seletor de set).

## Consequências

- Positivo: nenhuma heurística de string sobre set codes — zero risco de
  gerar um código que não existe. A consulta é honesta: só devolve resultado
  quando o catálogo já sincronizou a linha correspondente.
- Positivo: hoje a conversão só "funciona" de fato para PT/produtos `OP` —
  é o comportamento correto, não uma limitação a corrigir. Não tentar
  "consertar" isso para DE/FR/IT no futuro achando que é bug: **não existe
  código distinto para converter**, a menos que a Konami mude a política de
  impressão.
- Positivo: `PrintLanguageResolver` é um `Protocol` — o dia em que existir
  uma base própria de traduções de print, uma segunda implementação
  (ex.: `DatabaseTranslationPrintLanguageResolver`) troca por injeção no
  `ScanService`, sem mexer em matching, revisão ou API.
- Negativo: para a maioria das cartas em alemão/francês/italiano, a única
  coisa que o app registra sobre o idioma é `CollectionItem.language` — o
  print/set exibido continua sendo o inglês, porque é o único que existe no
  catálogo. Isso é uma limitação da fonte de dados (YGOPRODeck), não do app.
