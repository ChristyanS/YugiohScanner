# Yu-Gi-Oh! Collection Scanner

Scanner e gerenciador de coleção de cartas de Yu-Gi-Oh!: fotografe suas cartas,
deixe o OCR identificar nome e set number, e mantenha a coleção em um banco
SQLite local — utilizável pelo terminal ou por uma interface web.

O plano técnico completo está em [`docs/PLAN.md`](docs/PLAN.md).

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
| 8 | Interface Web | ⬜ |
| 9 | Calibração e performance | ⬜ |
| 10 | Fallback por LLM Vision (opcional) | ⬜ |
| 11 | Empacotamento e documentação | ⬜ |

## Requisitos

- Python 3.10 ou superior (com SQLite compilado com FTS5 — o instalador oficial
  do Windows já vem assim).

## Instalação

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # Linux/macOS

pip install -e ".[ocr,dev]"
```

Extras disponíveis:

| Extra | Traz | Quando usar |
|---|---|---|
| `ocr` | RapidOCR, Pillow, RapidFuzz | Motor de OCR padrão — sem dependência de sistema |
| `web` | FastAPI, Uvicorn, Jinja2 | Interface web |
| `tesseract` | pytesseract | Se você já tem o Tesseract instalado |
| `paddle` / `easyocr` | motores alternativos | Pesados; só se precisar |
| `llm` | SDK da Anthropic | Fallback por Claude Vision |
| `dev` | pytest, ruff, mypy | Desenvolvimento |

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
```

O `init` baixa ~14.500 cartas, ~44.500 prints e ~646 sets em cerca de 12
segundos, respeitando metade do rate limit da API. Depois disso a aplicação é
offline: só `sync` e o cache de imagens tocam a rede.

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
pytest -m ocr             # testes de OCR com fotos reais (opt-in)
ruff check . && ruff format --check .
mypy src
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

## Licença

MIT.
