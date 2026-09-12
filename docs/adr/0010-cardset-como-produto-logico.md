# ADR 0010 — `CardSet` é o produto lógico; `CardPrint.region` é a impressão regional

**Status:** aceito
**Data:** 2026-09-12
**Relacionado:** [ADR 0009](0009-idioma-do-print-por-regiao-existente.md),
[docs/proposta-i18n-cartas-e-sets.md](../proposta-i18n-cartas-e-sets.md) §2.4

## Contexto

A investigação em `docs/proposta-i18n-cartas-e-sets.md` levantou uma dúvida
de modelagem que o schema atual já resolve na prática, mas nunca de forma
consciente: quando um produto tem impressões regionais genuínas — o caso real
confirmado é `OP13-EN006` / `OP13-PT006`, mesma carta, mesmo número de
coleção, `set_name` "OTS Tournament Pack 13" / "OTS Tournament Pack 13
(POR)" — isso deveria ser **um produto com duas impressões** ou **dois
produtos independentes relacionados**?

Sem essa decisão registrada, um futuro "conserto" do schema poderia dar uma
linha de `CardSet` por região (`OP13-EN`, `OP13-PT` como sets separados),
duplicando o que hoje já funciona corretamente por um motivo que não estava
documentado.

## Decisão

`CardSet.set_code` (`db/tables.py`, ex. `"OP13"`, `"RA05"`) é o **produto
lógico** — o evento de lançamento, independente de região. `CardPrint.region`
é o que diferencia as **impressões regionais** desse mesmo produto, quando
elas existem de fato no catálogo sincronizado. Não é preciso mudar nada no
schema: `CardSet` já é chaveado pelo prefixo compartilhado, e é
`CardPrint.set_prefix` + `CardPrint.region` que já expressam "este print é a
versão em tal idioma daquele produto".

Isso só funciona porque a ligação nunca é inferida por string — é sempre um
dado que a API já trouxe (mesmo `card.id`, `set_name` visivelmente aparentado
como `"X"` / `"X (POR)"`). Ver ADR 0009 para a decisão irmã: o resolvedor de
print por idioma (`SiblingPrintLanguageResolver`) consulta essa relação, nunca
gera um código regional a partir do inglês.

## Consequências

- Positivo: nenhuma mudança de schema necessária — a modelagem atual já está
  certa. Este ADR só nomeia a decisão para que não seja "corrigida" por
  engano no futuro.
- Positivo: generaliza para qualquer região futura sem migração — se a
  YGOPRODeck um dia sincronizar `OP20-DE006`, ele automaticamente vira mais
  uma impressão regional do produto `OP20`, sem precisar de uma linha nova em
  `CardSet`.
- Negativo (aceito): a esmagadora maioria dos produtos não tem impressão
  regional nenhuma no catálogo — `CardSet` continua, na prática, "TCG em
  inglês" para quase tudo. Isso é limitação da fonte de dados (YGOPRODeck não
  modela produtos OCG como sets pesquisáveis, ver `proposta-i18n-cartas-e-
  sets.md` §2.5), não um defeito desta decisão.
