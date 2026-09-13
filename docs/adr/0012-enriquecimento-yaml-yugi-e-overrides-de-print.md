# ADR 0012 — Enriquecimento via `yaml-yugi` + tabela de overrides aprendida do uso

**Status:** aceito
**Data:** 2026-09-12
**Relacionado:** [docs/proposta-fontes-dados-catalogo.md](../proposta-fontes-dados-catalogo.md)
(diagnóstico completo, opções A-E, pros/contras), [ADR 0009](0009-idioma-do-print-por-regiao-existente.md),
[ADR 0010](0010-cardset-como-produto-logico.md)

## Contexto

`docs/proposta-fontes-dados-catalogo.md` mediu, com dados reais, que a
YGOPRODeck (única fonte hoje) não tem prints regionais para DE/FR/IT/PT fora
de produtos `OP*`, e não tem nenhum set OCG — enquanto o dataset comunitário
`yaml-yugi` (derivado do Yugipedia) tem dezenas de milhares de prints reais
nesses idiomas para praticamente todo o catálogo. O documento avaliou cinco
opções (A: trocar de base, B: cruzar dados/enriquecimento, C: heurística de
string — refutada, D: tabela de overrides aprendida do uso, E: híbrida B+D)
e recomendou a Opção E.

## Decisão

Seguir a **Opção E**: YGOPRODeck continua sendo a fonte primária (identidade
da carta, imagens, preços, API estável); `yaml-yugi` entra como uma segunda
etapa de sync, **opcional e isolada**, que só preenche o que a fonte
primária não cobre; e uma tabela de overrides captura, do próprio uso do
app, a correspondência carta+idioma → print sempre que um humano confirma
isso manualmente na revisão.

### B — Enriquecimento via `yaml-yugi`

Novo módulo `catalog_sources/yaml_yugi/` (paralelo a `ygoprodeck/`, mesma
separação de responsabilidade: cliente HTTP + schemas Pydantic + importador).
Roda como comando separado (`yugioh-scanner db enrich-i18n`), nunca dentro da
transação do `sync` principal — se falhar ou o formato mudar, o catálogo
primário não é afetado (mitigação do risco de bus factor documentado na
proposta, §3.2).

Regras de importação, diretamente derivadas da validação de risco §3.1 da
proposta:

1. **Nunca cria `Card` nova** — casa por `password == Card.id`; se a carta
   não existe no catálogo primário, a linha do `yaml-yugi` é ignorada. A
   identidade da carta continua vindo exclusivamente da YGOPRODeck.
2. **Nunca sobrescreve dado da YGOPRODeck.** `CardAltName`/`CardPrint` ganham
   uma coluna `data_source` (`'ygoprodeck'` por padrão). O importador só
   grava quando não existe linha da YGOPRODeck para aquela chave lógica;
   linhas já enriquecidas por uma execução anterior são atualizadas
   normalmente (idempotente).
3. **Nome:** só para os idiomas de `ALT_NAME_LANGUAGES` (mesma lista que a
   YGOPRODeck já usa) — o que exclui `es` de propósito, porque a validação
   mediu 0 prints `es` reais em todo o dataset apesar de 98% das cartas
   terem um nome em espanhol (proposta §3.1.3). Nomes sinalizados
   `is_translation_unofficial` são pulados sempre.
4. **Print/set:** para cada `(idioma, print)` em `sets`, resolve ou cria o
   `CardSet` (prefixo do produto) e cria o `CardPrint` (com `data_source =
   'yaml_yugi'`), reaproveitando `domain.setcode.parse_set_code` — que já
   lida corretamente com sufixos de uma letra (`SDY-G005`) e com prefixos
   totalmente diferentes de produto OCG (`DBWS-JP016`), sem exigir nenhuma
   mudança nesse módulo.

### D — Tabela de overrides aprendida do uso

Nova tabela `card_print_override` (`card_id`, `language`, `card_print_id`,
única por `(card_id, language)`). Alimentada automaticamente em
`ScanService.confirm_result` sempre que um humano confirma uma leitura
pendente/manual com um `card_print_id` explícito e um idioma diferente de
`EN` — o exato momento em que o app "aprende" uma correspondência que nem a
YGOPRODeck nem o `yaml-yugi` tinham.

`PrintLanguageResolver` (ADR 0009) já era um `Protocol` pensado para esta
extensão. Duas implementações novas em `matching/print_language.py`:

- `OverridePrintLanguageResolver` — consulta a tabela de overrides.
- `CompositePrintLanguageResolver` — encadeia resolvedores na ordem dada; o
  default do `ScanService` passa a ser
  `CompositePrintLanguageResolver([OverridePrintLanguageResolver(),
  SiblingPrintLanguageResolver()])`: uma correção aprendida do usuário
  sempre vence a heurística automática de "print irmão".

## Consequências

- Positivo: risco isolado de verdade — `enrich-i18n` é um comando separado,
  best-effort, que nunca impede `sync`/`scan`/`review` de funcionar mesmo se
  o `yaml-yugi` sumir ou mudar de schema.
- Positivo: a tabela de overrides funciona mesmo sem nunca rodar
  `enrich-i18n` — cobre exatamente o caso "nem fonte nenhuma tem esse
  print", que sempre vai existir em alguma escala.
- Positivo: `data_source` torna toda a proveniência auditável — um relatório
  futuro pode listar "quantos prints vieram de onde" sem heurística.
- Negativo (aceito): `enrich-i18n` baixa um arquivo de ~94 MB e precisa de
  uma etapa própria de cache condicional (ETag) para não repetir isso a
  cada execução — complexidade que a YGOPRODeck não exigia (ela pagina).
- Negativo (aceito): nomes/prints em espanhol ficam de fora do
  enriquecimento automático até existir uma forma melhor de os validar
  (proposta §3.1.3) — cartas físicas em espanhol continuam exigindo
  resolução manual, que a tabela de overrides (D) cobre de qualquer forma.
