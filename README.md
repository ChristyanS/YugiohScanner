# Yu-Gi-Oh! Collection Scanner

Scanner e gerenciador de coleção de cartas de Yu-Gi-Oh!: fotografe suas cartas,
deixe o OCR identificar nome e set number, e mantenha a coleção em um banco
SQLite local — utilizável pelo terminal ou por uma interface web.

Documentação: [`docs/architecture.md`](docs/architecture.md) (visão geral
atual do sistema), [`docs/adr/`](docs/adr/) (decisões individuais) e
[`docs/PLAN.md`](docs/PLAN.md) (o plano técnico original completo).

## Estado atual

| Fase | Escopo | Status |
|---|---|---|
| 1 | Fundação: projeto, configuração, logging, schema, migrações | ✅ |
| 2 | Sincronização com a API do YGOPRODeck | ✅ |
| 3 | Scanner + OCR | ✅ |
| 4 | Matching | ✅ |
| 5 | Coleção | ✅ |
| 6 | CLI completa | ✅ |
| 7 | Exportação | ✅ |
| 8 | Interface Web | ✅ |
| 9 | Calibração e performance | 🟡 parcial¹ |
| 10 | Fallback por LLM Vision (opcional) | ⬜ |
| 11 | Empacotamento e documentação | 🟡 parcial² |

¹ Performance (§20.5) e cobertura (§19.5) medidas e documentadas. Calibração
de confiança **medida contra 20 fotos reais** (2026-09-11) — resultado:
**40% de acerto top-1 do nome** (meta ≥80%), causa raiz identificada (leitura
degradada em fotos tiradas através de plástico de álbum, não carta solta
sobre fundo liso). Um bug real de ordenação de texto no OCR foi encontrado e
corrigido no processo. Limiares de confiança **não mudaram** — a calibração
mostrou que o problema é a leitura, não o limiar, e que a política atual já
não deixa nenhuma leitura ruim entrar sozinha na coleção. Detalhes completos:
`docs/adr/0001-limiares-de-confianca.md` e `tests/fixtures/cards/README.md`.
Ainda pendente: fotos de controle (carta solta, fundo liso) para isolar a
causa com mais confiança.

² README, `docs/architecture.md` e 8 ADRs (`docs/adr/`) prontos. O
`Dockerfile` foi escrito mas **não verificado**: o ambiente onde foi criado
não tinha Docker disponível — só o que dava para checar sem Docker
(instalação do pacote e CLI, ver comentário no topo do `Dockerfile`) foi
checado. `docker build`/`docker run` de verdade ficam pendentes.

## Requisitos

- Python 3.10 ou superior (com SQLite compilado com FTS5 — o instalador oficial
  do Windows já vem assim).

## Instalação

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # Linux/macOS

pip install -e ".[ocr,web,dev]"
```

`web` é necessário para rodar a suíte completa de testes (Fase 8 usa
`TestClient` do FastAPI nos testes de integração), não só para subir o
servidor.

Extras disponíveis:

| Extra | Traz | Quando usar |
|---|---|---|
| `ocr` | RapidOCR, Pillow, RapidFuzz | Motor de OCR padrão — sem dependência de sistema |
| `web` | FastAPI, Uvicorn, Jinja2 | Interface web |
| `tesseract` | pytesseract | Se você já tem o Tesseract instalado |
| `llm` | SDK da Anthropic | Fallback por Claude Vision (Fase 10, ainda não implementado) |
| `dev` | pytest, ruff, mypy | Desenvolvimento |

`paddle` e `easyocr` aparecem em `config.BUILTIN_OCR_PROVIDERS` e têm extras
declarados em `pyproject.toml`, mas os providers (`ocr/paddle_provider.py`,
`ocr/easyocr_provider.py`) **ainda não foram implementados** — escolher
`--provider paddle` hoje falha com um erro limpo (`OcrProviderError`), não um
crash. Só `rapidocr` e `tesseract` funcionam agora.

## Uso rápido

```bash
yugioh-scanner --help
yugioh-scanner init            # cria o banco e baixa o catálogo (~12s)
yugioh-scanner db status       # o que existe no banco agora
yugioh-scanner check-updates   # há catálogo novo? (1 requisição)
yugioh-scanner sync            # atualiza só se a versão remota mudou
yugioh-scanner sync --force    # reimporta mesmo sem mudança de versão
yugioh-scanner scan ./cartas   # lê, identifica e grava na coleção (padrão)
yugioh-scanner scan ./cartas --dry-run    # só mostra o que aconteceria
yugioh-scanner scan ./cartas --no-auto    # nada entra sozinho; tudo vira pendente
yugioh-scanner scan ./cartas --reprocess  # relê imagens já vistas (não reaplica)
yugioh-scanner scan ./cartas --interactive  # revisa as pendências assim que o scan termina
yugioh-scanner review                      # fila de revisão (confirma/rejeita, com candidatos)
yugioh-scanner scan-status                 # últimos scans e suas estatísticas

yugioh-scanner collection list --search "blue eyes"  # busca, filtro, ordenação
yugioh-scanner collection add "dark magician" --set-code SDK-001
yugioh-scanner collection remove 3 --qty 1
yugioh-scanner collection set-print 5 LOB-001   # resolve um item sem set definido
yugioh-scanner collection show 5                # carta + todos os prints conhecidos
yugioh-scanner collection stats
yugioh-scanner config show     # configuração efetiva (segredos mascarados)

yugioh-scanner export --list-profiles                       # perfis disponíveis e suas colunas
yugioh-scanner export --format csv                           # perfil padrão (ygoprodeck) no stdout
yugioh-scanner export --format csv --profile ygopocket -o coleção.csv
yugioh-scanner export --format txt --profile deck            # uma linha por cópia
yugioh-scanner export --format csv --profile full -o backup.csv --skip-unresolved

yugioh-scanner web                          # http://127.0.0.1:8000
yugioh-scanner web --port 8080 --reload     # dev: recarrega ao editar código
```

O `init` baixa ~14.500 cartas, ~44.500 prints e ~646 sets em cerca de 12
segundos, respeitando metade do rate limit da API. Depois disso a aplicação é
offline: só `sync` e o cache de imagens tocam a rede.

### Interface Web

Cinco telas (plano §10): dashboard (`/`), scanner com progresso ao vivo via
SSE e upload por drag-and-drop (`/scan`), revisão com atalhos de teclado
`1`-`5`/`Enter`/`S`/`X` (`/review`), coleção com filtro incremental e edição
inline (`/collection`) e detalhe de carta (`/cards/{id}`). FastAPI serve HTML
(Jinja2 + HTMX, sem build step — HTMX e Pico.css vendorizados em
`web/static/vendor/`, nada de CDN) e JSON (`/api/v1/*`) pelos **mesmos
serviços** da CLI. Bind em `127.0.0.1` por padrão; sem autenticação (plano
§21) — não exponha em `0.0.0.0` sem entender essa limitação.

Imagens de carta são cacheadas sob demanda em `data/images/` e servidas
localmente (`/api/v1/cards/{id}/image`) — nunca hotlink direto ao YGOPRODeck
(plano §0.2, §14.1). Verificado: com o cache quente, uma segunda requisição
pela mesma imagem não toca a rede.

### Idempotência do `scan`

Rodar `scan` na mesma pasta duas vezes não duplica nada: cada foto é
identificada pelo **hash do conteúdo**, não pelo caminho — renomear ou mover
arquivos não engana o sistema. Confirmado em produção contra o catálogo real:

```
1ª execução: 5 cartas identificadas e adicionadas, 1 imagem inválida isolada
2ª execução: 6/6 imagens puladas, coleção continua com 5 itens / 5 cópias
--reprocess: relê as 6, decisão continua "auto", mas 0 são reaplicadas
```

### Perfis de exportação

| Perfil | Formato | Verificação |
|---|---|---|
| `ygoprodeck` (padrão do CSV) | csv | 7 colunas confirmadas no fórum/suporte oficial (docs/PLAN.md §0.4) |
| `ygopocket` | csv, txt | Byte a byte contra exports reais fornecidos pelo usuário (`export-samples/`) |
| `full` | csv | Superset com os IDs internos — único com round-trip garantido (export → import → coleção idêntica) |
| `list` (padrão do TXT), `deck` | txt | Formatos próprios, não tentam imitar nenhum app externo |

`--skip-unresolved` omite itens sem print identificado em vez de exportá-los
com as colunas de set vazias. Todo CSV neutraliza injeção de fórmula
(`=`/`+`/`-`/`@` no início de uma célula vira `'=`/`'+`/... antes de gravar).

## Configuração

Copie `.env.example` para `.env` e ajuste o que precisar. Toda variável usa o
prefixo `YGS_`; nenhuma é obrigatória — os defaults funcionam para uso local.

```bash
cp .env.example .env
```

Segredos (como `ANTHROPIC_API_KEY`) só saem do `.env` ou do ambiente, nunca do
código, e são mascarados em logs e em `config show`.

## Desenvolvimento

```bash
pytest                    # suíte principal (rápida, sem rede e sem OCR real)
pytest --cov=src/yugioh_scanner   # com gate de cobertura (≥80%, plano §19.5)
pytest -m ocr             # testes de OCR com fotos reais (opt-in) + corpus da Fase 9
ruff check . && ruff format --check .
mypy src

python scripts/calibrate_thresholds.py       # calibra os limiares contra o corpus real
python scripts/benchmark_performance.py      # mede os alvos do plano §20.5
```

A suíte principal não toca na rede nem em motores de OCR de verdade: as
respostas da API são fixtures gravadas e o OCR é substituído por um provider
determinístico. `pytest -m ocr` roda o motor real contra cartas sintéticas e
mede taxas agregadas, não imagem por imagem.

### Qualidade do matching

Medido contra o catálogo real (14.524 cartas), com 60 cartas sorteadas e nomes
corrompidos do jeito que o OCR corrompe (troca `I`↔`1`, `O`↔`0`, truncamento,
espaço comido):

| Métrica | Resultado |
|---|---|
| Top-1 correto | 58/60 (97%) |
| **Erros que entrariam sozinhos na coleção** | **0** |
| Tempo por matching | 7,3 ms |
| Uso do tier 4 (fuzzy no catálogo inteiro) | 8 de 60 |

O número que mais importa é o segundo: as duas leituras erradas foram para
revisão manual, não para a coleção. A política prefere perguntar a errar.

### Performance (Fase 9)

Medido em 2026-09-10 com `scripts/benchmark_performance.py` (mesma máquina da
seção anterior). Tabela completa e a explicação de cada item que ficou acima
do alvo em `docs/PLAN.md` §20.5 — os dois achados relevantes:

- **Tier 4 do matching (fuzzy no catálogo inteiro) mede ~210 ms**, não os
  ~20-50 ms que o plano original previa: a implementação usa
  `rapidfuzz.process.extract` (thread único) em vez do
  `process.cdist(workers=-1)` descrito em §20.2. É o *fallback* mais raro da
  escada de tiers (tier 0/1 já atende o caso comum dentro do alvo) — achado
  registrado, correção não aplicada nesta fase.
- **Scan em paralelo não melhora com mais workers que o automático.** Medido
  com 3 workers (automático) *e* 8 (forçado): 8 workers foi **mais lento**
  (2,43 s/foto vs 2,22 s/foto) — núcleos disputados, não ociosos. Confirma a
  decisão de `WORKER_CORE_DIVISOR` já em `config.py`.

### Calibração de confiança (Fase 9 — bloqueada, não inventada)

`CONFIDENCE_AUTO`/`CONFIDENCE_REVIEW` continuam nos valores de julgamento de
engenharia das Fases 1-4 — **não** foram recalibrados porque isso exigiria
medir contra fotos reais suas, e nenhuma existe ainda no repositório (plano
§19.4 é explícito: as fotos são do usuário, não geradas nem baixadas). A
ferramenta está pronta e testada (`scripts/calibrate_thresholds.py` +
`tests/ocr/test_corpus_accuracy.py`, que pula sozinho sem corpus) — falta só
adicionar 15–25 fotos em `tests/fixtures/cards/` (ver o README de lá para o
formato). Contexto completo em `docs/adr/0001-limiares-de-confianca.md`.

### Sobre `--workers`

O OCR satura **banda de memória**, não CPU. Medido em um i5-9400F (6 núcleos):

| workers | tempo (10 cartas) | throughput |
|---|---|---|
| 1 | 37,2s | 0,27 cartas/s |
| 2 | 27,6s | 0,36 cartas/s |
| 4 | 29,3s | 0,34 cartas/s |
| 6 | 25,2s | 0,40 cartas/s |

Praticamente todo o ganho está em sair de 1 para 2. Por isso o default é
**metade dos núcleos**, e não `cpu-1`: mais processos custam memória e tempo de
spawn sem aumentar o throughput. Ajuste com `--workers` se sua máquina for
diferente.

## Solução de problemas

**`sqlite3.OperationalError: no such module: fts5`** — seu Python foi
compilado sem FTS5 (comum em builds Linux de distro antiga). O instalador
oficial do [python.org](https://www.python.org/) já vem com FTS5 no Windows e
no macOS; no Linux, use a versão do `deadsnakes` PPA (Ubuntu) ou compile com
`--enable-loadable-sqlite-extensions`. Confira com:
```bash
python -c "import sqlite3; print(sqlite3.connect(':memory:').execute(\"pragma compile_options\").fetchall())"
```
(procure `ENABLE_FTS5` na lista).

**`yugioh-scanner` diz que o banco não existe, ou está desatualizado** —
`db status` mostra o estado exato; `db upgrade` cria/atualiza. Nenhum comando
apaga dados sozinho: migrações destrutivas fazem backup em `data/backups/`
antes (plano §21).

**`OcrProviderError: provider de OCR desconhecido` ou `não está disponível`**
— ou o nome está errado (`rapidocr`, `tesseract` são os únicos implementados
hoje — ver tabela de extras acima), ou a dependência opcional não foi
instalada: `pip install -e ".[ocr]"` para o padrão, `.[tesseract]` mais o
próprio [Tesseract instalado no sistema](https://github.com/UB-Mannheim/tesseract/wiki)
(Windows) para o alternativo.

**RapidOCR falha ao instalar (`onnxruntime` sem wheel para sua plataforma)**
— acontece em Python muito novo ou arquitetura incomum (ARM64 Windows, por
exemplo). Confira se há build do `onnxruntime` para sua combinação exata de
SO/arquitetura/Python antes de tentar; `tesseract` é a alternativa mais
portável (exige instalar o Tesseract separadamente).

**`yugioh-scanner web` não sobe / `ImportError`** — dependências da web não
instaladas: `pip install -e ".[web]"`. Se a porta 8000 já estiver em uso,
`yugioh-scanner web --port 8080`.

**Caracteres estranhos (`?`, quadrados) no terminal do Windows** — o `cmd.exe`
clássico às vezes não suporta UTF-8; a CLI já degrada símbolos Unicode para
ASCII automaticamente quando detecta isso (✓ vira `+`), mas nomes de carta
acentuados podem continuar estranhos. Use o Windows Terminal ou PowerShell 7+,
que já vêm em UTF-8 por padrão.

**Scan não encontra nada, ou recusa a pasta** — confira `ALLOWED_SCAN_ROOTS`
em `config show`: se configurado, só pastas dentro dele são aceitas (plano
§21). Vazio (o padrão) aceita qualquer caminho local.

**Chave da Anthropic aparece nos logs** — não deveria: `config show` mascara
segredos e o logger redige o padrão `sk-ant-*` automaticamente. Se você vir
uma chave completa em algum lugar, é um bug — abra uma issue.

## Docker (opcional, não verificado)

```bash
docker build -t yugioh-scanner .
docker run -d --name yugioh-scanner -p 8000:8000 -v yugioh-data:/app/data yugioh-scanner init
docker run -d --name yugioh-scanner -p 8000:8000 -v yugioh-data:/app/data yugioh-scanner web --host 0.0.0.0
```

O `Dockerfile` foi escrito num ambiente sem Docker disponível para testar —
**`docker build`/`docker run` não foram verificados de verdade.** O que foi
verificado sem Docker (venv limpo, só com os arquivos que o `Dockerfile`
copia): `pip install -e ".[ocr,web]"` completa, `yugioh-scanner --help` e
`db upgrade` funcionam. A camada de container em si (imagem base,
`apt-get`, `ENTRYPOINT`, volume, rede) é o que falta confirmar — é o item
pendente do critério de aceitação da Fase 11 (`docs/PLAN.md`). `--host
0.0.0.0` é necessário dentro do container, mas a aplicação continua sem
autenticação ([ADR 0007](docs/adr/0007-sem-autenticacao.md)) — o único
isolamento é o mapeamento de porta do Docker.

## Licença

MIT.
