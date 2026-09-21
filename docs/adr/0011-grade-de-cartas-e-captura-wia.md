# ADR 0011 — Grade de cartas por foto (OpenCV opcional) e captura via scanner (WIA)

**Status:** aceito e implementado (CLI, Web e captura WIA contra hardware real)
**Data:** 2026-09-12 (implementação inicial); atualizado no mesmo dia após
teste contra scanner real (Canon G3010); atualizado em 2026-09-18 com deskew
por célula (ver "Deskew por célula" abaixo); atualizado em 2026-09-20 com
calibragem contra revisão real do Scan #11 (ver "Scan #11" abaixo)

## Contexto

Até aqui o pipeline inteiro (`scanner/{discovery,pipeline,executor,worker}.py`,
`services/scan_service.py`) assumia um invariante rígido: uma foto rende
exatamente uma leitura de OCR, um `MatchResult`, um `ScanResult`. O schema já
antecipava a mudança — `ScanResult` (`db/tables.py`) tinha desde a Fase 5 uma
docstring dizendo que era tabela separada de `ScanImage` "de propósito: o dia
em que uma foto puder conter várias cartas, isto vira 1—N sem migração de
dados" (`docs/PLAN.md` §22) — mas nada no código produzia essa segunda leitura.

Pedido do usuário: (1) permitir que uma foto contenha várias cartas
organizadas em grade (2x2, 3x3, 4x4 ou NxM irregular), detectando e
recortando cada carta automaticamente antes de rodar o pipeline de
identificação já existente em cada recorte; (2) permitir capturar a página
diretamente de um scanner/impressora físico, rodando a mesma detecção de
grade + identificação sobre a página capturada.

Duas decisões de arquitetura ficavam pendentes:

1. `images/preprocess.py` é deliberadamente Pillow-puro (`docs/PLAN.md` §3:
   "OpenCV só entra se o pré-processamento simples não bastar — não arrastar
   60 MB antes de precisar"). A heurística de borda de carta única
   (`detect_card_bounds`) rejeita qualquer recorte fora de 25%–97% da área do
   quadro — uma célula de grade 3x3 tem ~11%, e seria sempre rejeitada. Não
   dava para reaproveitá-la para segmentar uma grade.
2. Não existia nenhuma integração com hardware de scanner/impressora no
   projeto — território novo.

## Decisão

**Grade de cartas — implementada nesta fase.** Novo módulo
`images/grid.py` com `detect_grid_cells()`, baseado em detecção de contornos
via OpenCV (`cv2.findContours`/`adaptiveThreshold`), adicionado como extra
opcional `cv` (`opencv-python` + `numpy`) — nunca importado a menos que
`--grid` seja pedido, seguindo a mesma disciplina de import tardio dos
providers de OCR (`ocr/registry.py`): falha uma única vez, com hint de
instalação, antes de tocar no banco (`ScanService.scan()` chama
`ensure_cv_available()` antes de criar qualquer `ScanJob`). Mesma filosofia
de `detect_card_bounds`: sem confiança de grade, devolve `[]` e a foto cai no
caminho de carta única de sempre — recortar errado é pior que não recortar.

`ScanResult` ganhou `crop_index`, `crop_count` e `source_bbox_left/top/
right/bottom` (migração `0007`, `server_default` preservando `0`/`1`/`NULL`
para todas as linhas existentes) em vez de uma tabela nova — exatamente a
"migração trivial" prevista desde a Fase 5. O fan-out de uma foto em N
recortes acontece dentro do worker (`scanner/worker.py`:
`CropRegion`/`CropOutcome`), mantendo o paralelismo na granularidade de foto;
`ScanService._handle_outcome`/`_handle_crop` decidem e persistem cada recorte
como um resultado independente.

**Captura via scanner — implementada e testada contra hardware real.**
Windows apenas, via WIA (`pywin32`), sem abstração para SANE/TWAIN. WIA é
nativo do Windows e cobre a maioria dos multifuncionais; o alvo de
distribuição atual (`.exe` via PyInstaller, `desktop_launcher.py`) já é
Windows. Uma imagem capturada é só mais um arquivo numa pasta — a integração
se limita a "capturar → salvar em disco", reusando 100% do
`ScanService.scan(folder, grid=True, ...)` já existente para tudo depois
disso. `yugioh-scanner scan-device --list-devices` detectou de verdade o
scanner de um Canon G3010 conectado à máquina de desenvolvimento.

**Detecção manual do layout (`--grid-size`/`grid_size`) — adicionada após
teste com hardware real.** `detect_grid_cells()` (contorno via OpenCV) foi
calibrado e testado só contra cartas sintéticas (bordas limpas, fundo liso).
Contra uma digitalização real de 9 cartas (grade 3x3, mesmo Canon G3010),
achou só 2 candidatos — a arte de verdade das cartas (textura, cor, sombra,
reflexo) confunde a filtragem por contorno de um jeito que o corpus
sintético não previa. Em vez de afinar os limiares às cegas contra um único
exemplar de hardware, o usuário pediu a alternativa direta: informar o
layout que ele já sabe que existe (`"3x3"`). `images/grid.py::manual_grid_cells()`
faz isso — reaproveita `detect_card_bounds()` (a mesma heurística de borda
Pillow-only usada para carta única) para recortar a margem/mesa do scanner
ao redor do conjunto, e só depois divide em linhas×colunas iguais. Não
precisa de OpenCV: é aritmética + a detecção de borda que a carta única já
usa — `ensure_cv_available()` só é chamado quando o modo automático (sem
`grid_size`) é usado.

## Implementação

- `images/grid.py`: `ensure_cv_available()`, `CvUnavailableError`,
  `detect_grid_cells(image, min_cells=2, max_cells=16) -> list[BoundingBox]`.
  Filtra candidatos por área relativa ao quadro (2%–45%), aspecto ~59:86mm
  (±18%) e retangularidade (área do contorno / área do bbox ≥ 0.75); deduplica
  contornos aninhados (borda interna/externa da mesma carta) por IoU; ordena
  em ordem de leitura (linha a linha, esquerda→direita).
- `images/preprocess.py`: `prepare_image()` virou um wrapper fino sobre
  `load_image()` + `prepare_regions(image, region=None)` — a nova função
  aceita uma `region: BoundingBox | None` para recortar uma célula da grade
  antes do refino de sempre, permitindo carregar a foto uma vez só e recortar
  N vezes.
- `scanner/worker.py`: `_GRID_MODE` (estado por processo, como `_PROVIDER`),
  `CropRegion`/`CropOutcome`, `ScanOutcome.crops: list[CropOutcome]`. Com
  `--grid` desligado (padrão), `regions = [None]` sempre — o caminho de hoje é
  byte a byte o mesmo, só embrulhado numa lista de um item.
- `services/scan_service.py`: `scan(grid=False)`; `_apply_decision` grava
  `crop_index`/`crop_count`/`source_bbox` em cada `ScanResult`;
  `has_applied_result` (`repositories/scans.py`) passou a filtrar por
  `crop_index` — sem isso, o primeiro recorte aplicado de uma foto bloquearia
  os demais recortes da mesma `ScanImage`, tanto no scan original quanto num
  `--reprocess` seguinte (bug que os testes de integração de `TestGridMode`
  exercitam diretamente).
- CLI: `scan --grid/--no-grid` e `--grid-size LINHASxCOLUNAS` em
  `cli/scan_cmd.py`; `_render()` mostra `arquivo [i/N]` quando uma foto
  rendeu mais de um recorte. `scan-device` (`cli/capture_cmd.py`) tem as
  mesmas duas flags, com `--grid` ligado por padrão (captura de scanner
  tende a ter várias cartas).
- Captura WIA: pacote `capture/` (`base.py` com o `Protocol
  ScannerBackend`, `registry.py`, `wia.py` com automação `WIA.DeviceManager`
  via `win32com.client`), comando `scan-device`, extra `wia` (`pywin32`,
  marcador `sys_platform == 'win32'`).
- **Bug real corrigido**: o driver do Canon G3010 ignora o GUID de formato
  pedido (`wiaFormatJPEG`, `_WIA_FORMAT_JPEG` em `capture/wia.py`) e devolve
  **BMP** de qualquer jeito. Salvar isso direto como `.jpg` produzia um
  arquivo cujos bytes não batiam com a extensão — `looks_like_image()`
  (checagem de magic bytes) rejeitava a foto inteira como inválida assim
  que chegava no pipeline de OCR (sintoma relatado: "a foto é registrada
  com falha" só quando vinda do scanner real; funcionava normalmente com
  fotos do disco). Correção: `WiaScannerBackend.capture()` sempre reabre o
  arquivo bruto com Pillow (que detecta o formato de verdade pelos bytes,
  não confia na extensão) e regrava como JPEG antes de devolver o caminho.
  Também reduziu o tamanho do arquivo (BMP não comprimido de ~26 MB virou
  JPEG de ~4-18 MB). Regressão coberta por
  `test_normalizes_driver_format_mismatch_to_real_jpeg`
  (`tests/unit/test_wia_capture.py`), que fakeia o driver devolvendo BMP.
- Web (Fase C): `POST /api/v1/scans` aceita `grid`/`grid_size` (string, ex.
  `"3x3"`, parseada por `parse_grid_size`); `GET /api/v1/capture/devices` e
  `POST /api/v1/capture` expõem o mesmo pacote `capture/` no servidor;
  `result_to_dict()` (`web/serializers.py`) inclui `crop_index`/`crop_count`/
  `source_bbox`; `/scan` tem checkbox de grade, campo de texto para o layout
  manual e seção de captura (só aparece se `GET /api/v1/capture/devices`
  responder com sucesso — degrada em silêncio fora do Windows ou sem
  `pywin32`); `/scan/{job_id}` mostra uma coluna "Recorte" e agrupa
  visualmente linhas da mesma foto.
- **Ajuste real após uso em `/review`**: a primeira versão desenhava um
  retângulo "holofote" sobre a foto inteira indicando qual carta da grade
  estava em revisão. Dois problemas reais apareceram: (1) o destaque não
  acompanhava a troca de carta na fila — ele dependia do evento `load` da
  `<img>`, que não dispara de novo quando os N recortes de uma grade reusam
  a mesma URL de foto, então ficava preso na posição da primeira carta
  revisada; (2) mesmo funcionando, a carta ficava pequena demais dentro da
  foto inteira para comparar com os candidatos. Troca: `review.html` agora
  recorta e amplia só a célula em revisão via CSS (`width`/`height`/`left`/
  `top` calculados a partir de `source_bbox`, sem depender de evento de
  carregamento), preenchendo o quadro como se a carta tivesse sido
  fotografada sozinha.
- **Ajuste real após uso em `--pages`/captura em lote**: capturar N páginas
  numa chamada só (CLI) ou numa única requisição (Web) não dava tempo real
  de trocar a folha na mesa do scanner entre uma captura e outra — a
  segunda disparava antes da pessoa conseguir posicionar a próxima página
  (mesa plana, sem alimentador automático). Troca: CLI pausa e pede Enter
  entre páginas (`cli/capture_cmd.py`); Web virou uma página por clique,
  com o cliente reenviando o `folder` da resposta anterior
  (`CaptureStartBody.folder`) para acumular todas as páginas na mesma
  pasta antes de rodar o scan.

## Deskew por célula (2026-09-18)

**Pedido do usuário:** melhorar a calibragem de cartas na grade, porque as
cartas de verdade não ficam 100% alinhadas dentro da manga do fichário —
algumas ficam alguns graus tortas. Mapeou 3 digitalizações reais próprias
(`Scanner_20260913.png`, `(5)`, `(10)` — páginas 3x3 de fichário, 27 cartas
com gabarito de nome/set/passcode) para calibrar contra dado real, copiadas
para `tests/fixtures/grid_scans/`.

**Achado real, medido contra essas 3 fotos**: o problema não era só rotação.
`detect_card_bounds` (heurística Pillow de borda, `images/preprocess.py`) foi
calibrado para foto de carta única sobre mesa lisa; rodado dentro de uma
célula já recortada de uma página de fichário (costura da manga plástica,
carta vizinha, argola do fichário ao fundo), ele sub-ajusta — devolve quase a
célula inteira em vez da borda real da carta. Isso desloca `NAME_ROI`/
`CODE_ROI` para cima do texto de verdade, e a folga rotacional da carta
dentro da manga (a queixa original) piora ainda mais uma ROI já maldisposta.
Medido antes da correção contra `Scanner_20260913.png` (9 cartas, OCR real):
nome correto em 1/9 (a maioria voltou **vazia** — a ROI não pegava texto
nenhum), set code 5/9, passcode 8/9.

**Decisão:** `images/grid.py::locate_and_deskew_card(cell) -> Image | None`,
best-effort (não usa `ensure_cv_available()` — é melhoria opcional sobre o
caminho de grade, não um modo pedido explicitamente pelo usuário; sem
`cv2`/`numpy`, devolve `None` em silêncio). Acha o maior contorno da célula
(Otsu threshold invertido + dilate), pega o retângulo rotacionado de verdade
(`cv2.minAreaRect`), aplica os mesmos gates de confiança de
`detect_grid_cells` (aspecto 59:86mm, retangularidade) mais um piso de área
relativo à célula (não à página inteira), e endireita via
`cv2.getPerspectiveTransform`/`warpPerspective` usando os 4 cantos do
`boxPoints` — não por ângulo (`warpAffine`), para não depender da convenção
de sinal do ângulo do `minAreaRect`, que variou entre um protótipo e outro
nesta sessão. Sem confiança, devolve `None` e quem chama
(`scanner/worker.py::_prepare_crop`) cai byte a byte no
`prepare_regions(image, region=region.bbox)` de sempre — mesma filosofia de
"recortar errado é pior que não recortar".

`images/preprocess.py::prepare_regions` ganhou um parâmetro injetável
`code_roi` (padrão `CODE_ROI`, comportamento inalterado para quem não passa
nada). O recorte endireitado tem margem quase zero — bem mais justo que o
`detect_card_bounds` de sempre — então a mesma fração de altura cai em lugar
diferente: medido contra o corpus, a arte termina 10 pontos percentuais mais
cedo do que `CODE_ROI` assumia, então o topo antigo (0.665) capturava um
bocado de arte de alto contraste como ruído antes do código de verdade. Nova
constante `CODE_ROI_GRID = BoundingBox(0.40, 0.70, 0.98, 0.775)`, passada por
`_prepare_crop` só quando o deskew teve sucesso. `NAME_ROI`/`PASSCODE_ROI`
não precisaram de variante — já funcionaram bem contra o recorte endireitado
com as constantes de sempre.

**Resultado medido** (`python scripts/calibrate_grid.py`, corpus completo de
27 células, 3 fotos): **100% das cartas identificadas corretamente** pelo
matching (identidade resolvida via passcode como chave primária — decisão
anterior, não desta ADR); passcode lido corretamente em 85% das células
crua (o resto tem ruído de OCR que `domain/passcode.py::clean_passcode` ainda
resolve, por isso o match continua 100%); set code em 65% como string exata
(erros são principalmente confusão de caractere do OCR — `SDY.037` por
`SDY-037`, `P1002` por `PT002` — não posicionamento, e não impedem a
identificação porque o passcode já resolveu a carta). Ruído remanescente de
OCR (troca de caractere, cartas em japonês que o modelo padrão não lê) é
limitação do provider de OCR, não de calibragem/alinhamento — fora do escopo
desta mudança.

## Scan #11: calibragem contra revisão real (2026-09-20)

**Pedido do usuário:** usar o Scan #11 (job real do app, 11 fotos 3x3, 99
células) para calibragem — o usuário revisou manualmente todas as 99 células
em `/review`, então cada decisão final (`confirmed`/`pending`/`rejected`)
é gabarito confiável: 88 confirmadas e corretas, 4 pendentes (identidade
certa, só travadas por um bug), 7 rejeitadas por serem fundo de carta (não
devem entrar na coleção).

**Bug real encontrado e corrigido: confirmação travava em `pending` para
sempre.** As 4 células pendentes tinham em comum um código lido com o "T" de
uma região de 2 letras confundido com "1" pelo OCR (`SR06-PT002` →
`SR06-P1002`, `SDSB-PT016` → `SDSB-P1016`, `SR06-PT008` → `SR06-P1008`).
`domain/setcode.py::_split_region` reconhece região de 1 letra pra prints
europeus antigos de verdade (`PSV-E088`) — e "P" sozinho bate nessa mesma
regra (resto só com dígitos). Sem confirmação do catálogo,
`matching/resolver.py::resolve()` aceitava esse "P" não confirmado como
`detected_region`, que virava `ScanResult.detected_language="P"` — um valor
de 1 letra que o CHECK de `card_print_override` rejeita (exige 2-3 letras).
Ao confirmar a revisão, o `INSERT` estourava `IntegrityError` **dentro do
`flush()` de `mark_applied`**, desfazendo a transação inteira (decisão,
`collection_item`, tudo) sem nenhum erro visível na tela — a leitura voltava
pra fila de pendências como se nada tivesse acontecido. Corrigido em duas
camadas: `PrintResolver.resolve()` só confia numa região de 1 letra depois
de validada contra o catálogo (mesmo espírito de "recortar errado é pior que
não recortar"); `ScanService.confirm_result` também passou a cair para
`DEFAULT_LANGUAGE` em vez de propagar um `language` malformado (2-3 letras,
maiúsculo) — cobre tanto leituras antigas já gravadas com o valor quebrado
quanto um `language` arbitrário vindo da API. Testado contra uma cópia do
banco real: as 4 confirmações que travavam antes da correção aplicam
normalmente depois.

**Corpus estendido com dado real de uso, não só de teste.** 9 das 11 fotos
do Scan #11 foram copiadas para `tests/fixtures/grid_scans/` (2 eram bytes
idênticos a `scan_05.png`/`scan_10.png` — a mesma página do fichário foi
digitalizada de novo entre as duas sessões — excluídas para não contar a
mesma foto 2x). Gabarito construído a partir da revisão humana: nome/print
das 88 confirmadas lidos direto de `card_print.set_code_full`; as 4
pendentes tiveram a identidade inferida por carta irmã idêntica na mesma
foto (mesmo passcode); as 7 de fundo de carta viram `{"card_back": true}` —
`scripts/calibrate_grid.py` ganhou essa categoria: em vez de contar como erro
de nome/set/passcode, verifica só que o matching não autoconfirma o fundo
como se fosse uma carta de verdade (`Decision.AUTO`).

**Resultado medido** (corpus completo, 14 fotos / 108 células — 27 antigas +
81 do Scan #11): **nome identificado corretamente em 96,0%** das 101 células
de carta (identidade real — é o que decide o que entra na coleção); passcode
cru batendo em 73,3%; set code como string exata em 70,7-71,0%; **100% dos
7 fundos de carta corretamente não autoconfirmados** (nenhum falso positivo).

**Achado real, novo nesta sessão: confusão sistemática de glifo entre
"P"/"T" e "1"/"I" na região do set code.** Cerca de 15 das ~30 células com
set code errado nesta medição têm exatamente esse padrão — não é ruído
aleatório, é o formato pequeno/itálico do set code no OCR (RapidOCR) lendo
"T" como "1" (`SR06-PT002`→`P1002`, `RA05-PT026`→`P1026`, `SR06-PT008`→
`P1008`) ou como "I" (`SDSB-PT021`→`PI021`, `PHHY-PT029`→`PI029`,
`DASA-PT019`→`PI019`), às vezes "P" como "1" (`SR06-PT011`→`1T011`,
`RA05-PT009`→`1T009`) e, mais raro, os dois juntos (`SDCS-PT001`→`TT001`).
`domain/setcode.py::correction_variants` hoje só corrige dígitos↔letras
dentro do **número** (letra→dígito) e do **prefixo** (dígito→letra) — nunca
dentro da **região** de 2 letras, que é onde esse padrão bate. Um achado
secundário relacionado: a correção do prefixo também não é só numa direção —
`SR06`→`SRO6` (dígito lido como letra) e `SDY`→`SOY`/`30Y` (letra lida como
dígito) apareceram os dois no mesmo corpus, contrariando a suposição de hoje
de que só uma direção vale por posição. Nenhum dos dois vira identidade
errada (o passcode resolve a carta certa de qualquer jeito, 96% de nome
correto é prova disso) — o custo real é a carta entrar com print/raridade
não resolvidos, exigindo revisão manual extra que uma correção melhor de
região evitaria. **Pendente**: estender `correction_variants` para tentar
`1`/`I`↔`T` (e `1`↔`P`) especificamente na posição da região, e permitir as
duas direções de correção no prefixo — não implementado nesta sessão, fica
registrado para calibragem futura.

## Consequências

- Positivo: nenhuma mudança de comportamento para o caso comum (uma foto,
  uma carta) — verificado por teste de regressão dedicado
  (`test_single_card_photo_with_grid_enabled_is_unaffected`).
- Positivo: sem custo de dependência para quem não usa `--grid` — `cv2` só é
  importado sob pedido explícito.
- Mudança de contrato a registrar: `ScanRunReport.auto_added`/`pending`/
  `failed` passam de granularidade por-foto para por-recorte. Numericamente
  idêntico no caso de carta única (1 foto = 1 recorte), mas quem consome
  `--json` deve saber que uma foto de grade agora aparece como N linhas em
  `results`, todas com o mesmo `file`.
- Confirmado (não mais hipotético): `detect_grid_cells()` **não é confiável
  em fotos reais** — 2 de 9 cartas detectadas contra uma digitalização real
  de uma grade 3x3. `--grid-size`/`grid_size` é hoje o caminho recomendado
  para uso com scanner de verdade; a detecção automática por contorno segue
  disponível para quem não sabe o layout de antemão, mas deve ser tratada
  como best-effort, não como o caminho principal.
- Positivo: `manual_grid_cells()` não precisa do extra `cv` — quem só usa
  `--grid-size` nunca instala OpenCV.
- Pendente: afinar `detect_grid_cells()` contra um corpus real (hoje só há
  um exemplar de hardware testado); a suíte com OpenCV de verdade fica
  marcada `@pytest.mark.ocr`, opt-in.
