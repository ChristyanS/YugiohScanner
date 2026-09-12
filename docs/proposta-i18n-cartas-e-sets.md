# Proposta: internacionalização de cartas e sets (cartas traduzidas + set codes regionais)

**Status:** proposta para discussão (ainda não é ADR aceito)
**Data:** 2026-09-12
**Relacionado:** [ADR 0009](adr/0009-idioma-do-print-por-regiao-existente.md) (já aceito, cobre parte do problema 2)

## Metodologia

Nada abaixo é suposição. Toda afirmação sobre o comportamento da API foi
validada com chamadas reais a `https://db.ygoprodeck.com/api/v7/` durante esta
análise (12/09/2026), além da leitura do [API Guide oficial](https://ygoprodeck.com/api-guide/).
Os comandos `curl` estão citados para poderem ser reexecutados e conferidos.

---

## Parte 1 — Cartas traduzidas

### 1.1 O caso "Vanquish Soul Razen" — confirmado

```
GET /cardinfo.php?name=Vanquish%20Soul%20Razen          → 200, id=29302858, só card_sets -EN
GET /cardinfo.php?name=Vanquish%20Soul%20Razen&language=pt → 400 "No card matching your query"
GET /cardinfo.php?id=29302858&language=pt                → 400 "No card matching your query"
GET /cardinfo.php?id=29302858&language=de                → 400 "No card matching your query"
GET /cardinfo.php?id=29302858&language=fr                → 400 "No card matching your query"
GET /cardinfo.php?id=29302858&language=ja                → 400 "No card matching your query"
```

Confirmado: a carta **não tem nenhuma tradução cadastrada na YGOPRODeck**, em
nenhum idioma — nem por nome, nem por `id`. Isso descarta a hipótese "a carta
existe mas não pode ser localizada pelo nome traduzido" (b): o problema não é
de busca, é de **dado ausente**. É a hipótese (a): a YGOPRODeck simplesmente
não tem a tradução dessa carta cadastrada, independente de existir ou não uma
impressão física em português. A base deles cobre "mais de 9000 cartas
traduzidas" por idioma — não é o catálogo inteiro (~14.500 cartas), e cartas
recentes/de nicho são as mais prováveis de ficar de fora.

**Comportamento da API para carta sem tradução: HTTP 400, mesmo por `id`.**
Isso é importante — significa que `language=xx` não é um parâmetro de exibição
com fallback silencioso para inglês; é um **filtro de existência**. Se a
tradução não existe, a carta inteira desaparece da resposta, mesmo consultando
pelo identificador estável.

### 1.2 Contraprova: carta com tradução (Dark Magician / Mago Negro)

```
GET /cardinfo.php?id=46986414              → name="Dark Magician"
GET /cardinfo.php?id=46986414&language=pt  → name="Mago Negro", name_en="Dark Magician"
GET /cardinfo.php?id=46986414&language=de  → name="Dunkler Magier", name_en="Dark Magician"
GET /cardinfo.php?id=46986414&language=ja  → name="ブラック・マジシャン", name_en="Dark Magician"
GET /cardinfo.php?id=46986414&language=ko  → name (coreano), name_en="Dark Magician"
```

Dois achados relevantes que **o código atual ainda não aproveita**:

1. **`id` é um identificador global estável entre idiomas.** Em toda consulta
   acima, `id=46986414` é o mesmo valor, e é o mesmo `id` que aparece no
   `card_sets`/`card_images`/`ygoprodeck_url` (que continuam em inglês). É a
   chave certa para "mesma carta, independente do idioma" — e é exatamente o
   que `Card.id` já usa hoje (`db/tables.py:89`).
2. **A resposta traduzida inclui `name_en`** — o nome em inglês, de graça, no
   mesmo payload. Hoje `ApiCard` (`ygoprodeck/schemas.py:88`) não declara esse
   campo (é descartado por `extra="ignore"`) e `alt_name_row`
   (`ygoprodeck/importer.py:132`) não o grava. Não é estritamente necessário
   (o `card_id` já faz o vínculo), mas é uma verificação de integridade barata
   — serviria para detectar, no futuro, uma tradução que a API vinculou à
   carta errada.

### 1.3 `konami_id` vs `id` (passcode) — quando cada um serve

`konami_id` é estável entre idiomas (`4041` em todos os testes acima) — mas
**não é o mesmo conceito que `id`**. Pelo próprio guia da API: *"O konami_id
não é o passcode"*. Na prática:

- `id` (passcode/8 dígitos) identifica **uma impressão de arte/texto
  específica** — é o que está fisicamente escrito na carta e o que a
  YGOPRODeck usa como chave de tudo (`card_images`, `card_sets`).
- `konami_id` identifica **a entrada da carta no banco oficial da Konami**,
  que pode agrupar mais de um `id` (ex.: reimpressões com erratas que ganham
  um passcode novo, mas continuam "a mesma carta" para efeito de banimento/
  formato).

Para o propósito deste sistema (relacionar o que foi fotografado a um
registro), **`id` é o identificador correto** — é o que a API já usa como
chave estrangeira entre carta e tradução, e é o que `Card.id` já modela.
`konami_id` vale manter como está (já é guardado em `Card.konami_id`,
`db/tables.py:114`) só como metadado auxiliar/diagnóstico, não como chave.

### 1.4 Japonês e coreano: achado que contraria a hipótese do usuário — e contraria o próprio guia da API

O guia oficial e a própria mensagem de erro da API dizem que só `fr`, `de`,
`it`, `pt` são aceitos:

```
GET /cardinfo.php?id=46986414&language=xx
→ 400 "No valid language set. This API accepts ... 'fr', 'de', 'it' or 'pt' ..."
```

Mas, testando na prática:

```
GET /cardinfo.php?id=46986414&language=ja  → 200, nome em japonês
GET /cardinfo.php?id=46986414&language=ko  → 200, nome em coreano
GET /cardinfo.php?id=46986414&language=JA  → 200 (case-insensitive)
GET /cardinfo.php?id=46986414&language=jp  → 400 (só "ja", não "jp")
GET /cardinfo.php?id=46986414&language=es  → 400 (espanhol não existe)
GET /cardinfo.php?id=46986414&language=zh  → 400
```

**`ja` e `ko` funcionam de fato, mas não estão documentados nem confirmados
pela própria API** (a mensagem de erro que ela mesma devolve para um valor
inválido está desatualizada/incompleta). Isso muda a resposta à pergunta
"japonês não é idioma suportado pelo endpoint multilíngue" — no nível de
**nome/descrição da carta**, é suportado, só que de forma não-oficial. No
nível de **sets/produtos OCG**, não é (ver §2.5) — são dois problemas
diferentes que a pergunta original tratava como um só.

Consequência prática: **não dá para hardcodar a lista de idiomas a partir do
guia** — ela já está desatualizada em relação ao comportamento real. E não dá
para depender da mensagem de erro da API para descobrir a lista, porque ela
também está errada.

### 1.5 Onde isso quebra no código hoje

```python
# db/tables.py:167
ALT_NAME_LANGUAGES = ("FR", "DE", "IT", "PT")
...
_check_in("language", ALT_NAME_LANGUAGES, "ck_card_alt_name_language")  # linha 206

# config.py:209
valid = {"FR", "DE", "IT", "PT"}  # duplicado, mesmo comentário reconhece isso
```

Hoje, mesmo que o `sync` pedisse `language=ja`, a gravação em
`CardAltName` **falharia no CHECK constraint** do banco. O sistema já está
estruturalmente pronto para qualquer idioma (`card_alt_name.language` é
`String(8)`, o `Protocol` de matching não assume nenhum idioma específico) —
só essas duas listas fechadas impedem generalizar.

### 1.6 Proposta — Parte 1

1. **Trocar a lista fechada por uma lista configurável, não por um novo
   hardcode maior.** Não adicionar `"JA", "KO"` a `ALT_NAME_LANGUAGES` e
   declarar vitória — isso repete o mesmo erro (confiar em uma lista estática
   que já provamos estar desatualizada). Duas mudanças:
   - Remover o `CHECK` de `card_alt_name.language` (ou trocá-lo por uma
     validação frouxa — 2 a 3 letras maiúsculas) e validar a lista apenas em
     `config.sync_alt_languages`, num único lugar.
   - Adicionar uma rotina de **probe** (não de confiança na doc): antes do
     sync, tentar `cardinfo.php?id=<card_conhecido>&language=<code>` para cada
     idioma candidato e aceitar só os que respondem 200. Isso substitui
     "confiar no guia" por "confiar na API ao vivo", que é a fonte real da
     verdade (a diferença entre `ja`/`ko` funcionarem e a doc dizer que não é
     a prova cabal disso).
   - Trocar o default de `sync_alt_languages` de `["FR","DE","IT","PT"]` para
     incluir `"JA","KO"` **depois** de validar com o usuário que ele quer o
     custo de sync adicional (mais ~2 idiomas × ~14.500 cartas em requisições).
2. **Gravar `name_en`** em `CardAltName` (campo novo, opcional) — não como
   chave de nada, só como conferência: se um dia `name_en` divergir do
   `Card.name` para o mesmo `card_id`, é sinal de inconsistência na fonte, não
   bug nosso.
3. **Comportamento de UI/exportação para carta sem tradução (o caso Razen):**
   já é o estado natural do sistema hoje — sem linha em `card_alt_name`,
   a carta simplesmente aparece só em inglês, sem quebrar nada
   (`CardAltName` é populada à parte de `Card`; a ausência de uma linha não é
   erro). A única melhoria de produto sugerida: indicar na tela (badge
   discreto) "sem tradução para PT/DE/FR/JA/KO disponível na fonte" quando o
   usuário está navegando/revisando em um idioma que não tem `alt_name` — hoje
   o app mostra o inglês silenciosamente, o que é correto, mas pode confundir
   quem não sabe que a ausência é da fonte de dados, não um bug de busca.
4. **Não tratar `ja`/`ko` como suportados com a mesma confiança que
   `fr`/`de`/`it`/`pt`.** Como não são documentados oficialmente, tratá-los
   como *best effort*: se a API parar de aceitá-los ou mudar o formato sem
   aviso (não há changelog público garantido para comportamento
   não-documentado), o sync deve degradar (logar e pular esse idioma), nunca
   falhar o sync inteiro. Revalidar essa suposição periodicamente (ex.: no
   início de cada sync, o probe do item 1 já serve como revalidação
   contínua).

---

## Parte 2 — Sets e códigos regionais

Boa notícia: a pergunta 2 (o problema mais importante, segundo o usuário) já
tem uma investigação real e uma decisão registrada no
**[ADR 0009](adr/0009-idioma-do-print-por-regiao-existente.md)** deste mesmo
repositório, com dados do catálogo sincronizado (44.517 prints). Eu revalidei
as conclusões dela ao vivo contra a API hoje e elas se sustentam — e ampliei
para cobrir explicitamente os pontos que a pergunta original levanta (OCG/JP,
generalização além de PT).

### 2.1 A hipótese "trocar EN por PT/FR/DE/IT/JP no código" está errada — confirmado de novo

```
GET /cardsetsinfo.php?setcode=RA05-EN134   → 200, "Vanquish Soul Razen", Rarity Collection 5
GET /cardsetsinfo.php?setcode=RA05-PT134   → 400 "No card matching your query"
```

`RA05-PT134` **não existe** — não é uma questão de a API não "aceitar" o
código, é que esse produto nunca foi lançado com uma numeração em português.
Isso vale para a esmagadora maioria dos produtos modernos: cartas físicas em
alemão/francês/italiano/português usam **o mesmo código `-EN`** que a versão
inglesa. Trocar a letra da região no código geraria um código plausível mas
inexistente — exatamente o risco que o ADR 0009 já identificou e evitou.

### 2.2 Quando a relação EN→PT existe de verdade: produtos `OP` (OTS Tournament Pack)

```
GET /cardsetsinfo.php?setcode=OP13-EN006 → set_name="OTS Tournament Pack 13"        (mesmo id=1561110)
GET /cardsetsinfo.php?setcode=OP13-PT006 → set_name="OTS Tournament Pack 13 (POR)"  (mesmo id=1561110)
```

Aqui sim existem **duas linhas de produto genuínas**: mesma carta
(`id` idêntico), mesmo número de coleção (`006`), regiões diferentes
(`EN`/`PT`), inclusive com `set_name` marcando explicitamemente a variante
("(POR)"). Isso é o **único padrão em que a numeração é preservada entre
idiomas na prática** — mas é um padrão específico de um produto (`OP*`,
distribuído oficialmente com versão em português desde ~2020), não uma regra
geral do sistema de códigos.

**Conclusão para a arquitetura:** não existe uma função `f(código_en, idioma)
→ código_regional` confiável. A única fonte de verdade é "o catálogo já tem
essa linha ou não tem" — que é exatamente o que `SiblingPrintLanguageResolver`
(`matching/print_language.py`) já faz, consultando por
`(card_id, set_prefix, number, region)` em vez de gerar strings. **Essa parte
da arquitetura já está correta e não precisa mudar** — só precisa continuar
sendo alimentada pelo sync (que já traz qualquer região que a API listar,
sem filtro de idioma).

### 2.3 Respondendo às perguntas específicas do usuário sobre sets

| Pergunta | Resposta validada |
|---|---|
| Como os códigos oficiais funcionam entre idiomas? | Não há transformação de string confiável. O código é um dado do produto, não uma função do idioma. |
| O número final (`134`) representa a mesma carta entre idiomas? | Só quando o produto regional existe de fato (ex. `OP*`) — e nesses casos, sim, o número é preservado. Fora disso, a pergunta não se aplica: não existe outro número para comparar. |
| `EN, PT, FR, DE, IT, JP` têm relação confiável? | Não. `PT` é a única região com produtos TCG dedicados de fato (`OP*`); `DE/FR/IT` não têm nenhum produto com código de set próprio (cartas físicas nesses idiomas usam o código `-EN`); `JP` é OCG, um sistema de numeração paralelo e independente (ver §2.5). |
| Existem casos em que a numeração muda entre idiomas? | Não se aplica no sentido perguntado — quando existe par regional (`OP*`), a numeração **não muda**. O que muda é a existência do próprio produto. |
| Existem produtos lançados só em certos territórios? | Sim, nos dois sentidos: `OP*` com sufixo `PT` é América Latina/Brasil-específico (não existe `OP13-DE006`, por exemplo); e o inverso — a esmagadora maioria dos produtos OCG nunca sai em TCG e vice-versa. |
| Como lidar com produtos OCG/Japão? | Ver §2.5 — a API não carrega dado de set/produto OCG neste endpoint, independente do parâmetro `language`. É uma lacuna de fonte de dados, não de modelagem. |
| Como identificar que duas cartas pertencem ao mesmo produto lógico com códigos diferentes? | Só por dado explícito already presente na resposta da API (mesmo `id` + `set_name` visivelmente aparentado, ex. `"X"` e `"X (POR)"`) — nunca por heurística de string sobre o código. |
| Set único global com versões linguísticas, ou vários sets independentes relacionados? | Ver §2.4. |

### 2.4 Modelo de dados: "produto" vs "impressão regional"

A pergunta "o set deve ser um produto único global ou vários sets
independentes relacionados" já está, na prática, resolvida corretamente pelo
schema atual — só falta nomear o conceito explicitamente:

- `CardSet` (`db/tables.py:213`, chave = `set_code`, ex. `RA05`, `OP13`) é o
  **prefixo do produto** — mas hoje ele é tratado como se `EN` fosse
  implícito. Na prática, `OP13-EN*` e `OP13-PT*` deveriam ser entendidos como
  duas **impressões regionais do mesmo produto lógico "OTS Tournament Pack
  13"**, não dois produtos. Hoje isso já funciona por acidente, porque
  `CardSet.set_code` (`"OP13"`) é o prefixo compartilhado e `CardPrint.region`
  distingue a impressão — ou seja, **o modelo já é "um produto, N impressões
  regionais"**, só não está documentado como decisão consciente.
- Recomendação: não mudar o schema. Só formalizar essa leitura num ADR
  (ou nesta proposta) para que ninguém no futuro "corrija" o schema achando
  que `CardSet` devia ter uma linha por região — isso quebraria a modelagem
  que já funciona e duplicaria produtos que são, na origem, o mesmo evento de
  lançamento.

### 2.5 OCG / Japão — a lacuna real

```
GET /cardinfo.php?id=46986414&language=ja  → card_sets ainda são só CT13-EN003, LOB-EN005, RA05-EN083 ...
                                              (59 entradas, 0 em formato OCG)
GET /cardsets.php?language=pt              → parâmetro ignorado, devolve o catálogo TCG completo em inglês
```

Confirmado: **o parâmetro `language` só afeta nome/descrição da carta —
nunca afeta `card_sets`.** Mesmo pedindo a carta em japonês, os sets
retornados continuam sendo exclusivamente os produtos TCG em inglês. A
YGOPRODeck, pelo menos neste endpoint, **não modela produtos OCG como sets
pesquisáveis** — o que existe de OCG é só `ocg_date` e `formats` (metadados
soltos), não uma lista de impressões com código de coleção.

Isso significa que, para cartas japonesas físicas, **não existe hoje nenhuma
fonte dentro da YGOPRODeck para resolver "qual card_print é este print
japonês"** — o gap não é de parsing de código (`domain/setcode.py` já
reconhece `JP`/`JA` como região, `domain/setcode.py:29-30`), é de **dado
inexistente na origem**. O sistema já lida bem com esse caso genérico —
`CollectionItem.card_print_id` aceita `NULL` exatamente para "sei qual carta
é, não sei qual print" (comentário em `db/tables.py:299`) — então uma carta
japonesa cai nesse estado automaticamente, sem exigir nenhuma mudança.

**Proposta:** não tentar resolver isso "inventando" set codes OCG a partir do
inglês (seria repetir o erro que o ADR 0009 já evitou, e OCG tem um sistema de
numeração de produto totalmente independente do TCG — nem a contagem de sets
é a mesma). Se o usuário quiser, no futuro, matching de set também para
cartas OCG, a solução é uma **segunda fonte de dados** (ex. um dataset
comunitário de sets OCG, tipo YGOResources/Yugipedia) plugada atrás do mesmo
`Protocol` que já existe (`PrintLanguageResolver`) ou de um novo
`OcgSetResolver` análogo — sem mexer no restante da arquitetura. Isso é
trabalho futuro, não bloqueador: hoje a carta japonesa já é catalogável
corretamente (carta certa, print em aberto).

---

## Resumo executivo das mudanças propostas

| # | Mudança | Arquivo(s) | Status |
|---|---|---|---|
| 1 | Trocar `CHECK` fechado de `card_alt_name.language` por validação frouxa + lista viva em `config` | `db/tables.py`, `config.py`, migração `0006` | **Implementado** — CHECK agora é de formato (2-3 letras), `ALT_NAME_LANGUAGES` inclui `JA`/`KO` por decisão explícita do usuário (custo de sync aceito) |
| 2 | Rotina de *probe* de idiomas aceitos pela API antes do sync, em vez de confiar na lista da doc | `ygoprodeck/client.py` (`probe_language(s)`), `cli/db_cmd.py` (`db probe-languages`) | **Implementado** — comando de diagnóstico, não bloqueia o sync normal |
| 3 | Gravar `name_en` em `CardAltName` como campo de conferência | `ygoprodeck/schemas.py`, `ygoprodeck/importer.py`, migração `0006` | **Implementado** |
| 4 | Indicador de UI "sem tradução disponível na fonte" quando não há `alt_name` no idioma ativo | `web/templates/card_detail.html` | **Já existia** — `CatalogService.localize()`/`is_translated` já cobriam isso antes desta proposta |
| 5 | Formalizar em ADR a leitura "CardSet = produto lógico, CardPrint.region = impressão regional" | [ADR 0010](adr/0010-cardset-como-produto-logico.md) | **Implementado** |
| 6 | Nenhuma mudança no resolvedor de print por idioma — `SiblingPrintLanguageResolver` (ADR 0009) já está correto e genérico | — | Confirmado, sem alteração |
| 7 | Não implementar geração/heurística de set code para OCG; deixar `card_print_id = NULL` como estado válido para cartas japonesas, com porta aberta para uma segunda fonte de dados no futuro | — (decisão de não fazer) | Confirmado, sem alteração |

Depois de rodar `yugioh-scanner db upgrade` com a migração `0006`, o próximo
`yugioh-scanner sync` (ou `sync --force`) já baixa nomes/descrições em `JA` e
`KO` por padrão, além de `FR/DE/IT/PT`. `db probe-languages` revalida a
qualquer momento se a API ainda aceita esses códigos não-documentados.
