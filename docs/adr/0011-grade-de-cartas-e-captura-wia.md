# ADR 0011 — Grade de cartas por foto (OpenCV opcional) e captura via scanner (WIA)

**Status:** aceito e implementado (CLI, Web e captura WIA contra hardware real)
**Data:** 2026-09-12 (implementação inicial); atualizado no mesmo dia após
teste contra scanner real (Canon G3010)

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
