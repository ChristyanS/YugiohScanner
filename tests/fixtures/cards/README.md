# Corpus de calibração (Fase 9)

Este diretório fica vazio até que fotos **reais** sejam adicionadas — o plano
(§19.4) é explícito: *"fotos são suas, ficam no repositório"*. Nada aqui é
gerado ou baixado automaticamente; sintético não serve para calibrar (ver
`tests/ocr/test_real_ocr.py` para o porquê).

## O que adicionar

15–25 fotos suas, variadas de propósito — cada uma testando uma condição
diferente que uma foto de celular real produz:

- carta com sleeve (reflexo, distorção leve)
- carta com holografia/foil
- ângulo levemente torto (não perfeitamente de frente)
- carta antiga sem "set code" legível (`LOB` sem prefixo de região, cartas
  muito velhas)
- carta moderna com código completo (`RA01-PT045`, por exemplo)
- iluminação ruim (sombra, contraluz)
- pelo menos uma carta cujo nome é parecido com o de outra (testa o
  desempate por margem, plano §7.4)

Formatos aceitos: `.jpg`/`.jpeg`/`.png` (os mesmos do scanner).

## `expected.json`

Um array com uma entrada por foto:

```json
[
  {"file": "blue_eyes_lob.jpg", "name": "Blue-Eyes White Dragon", "set_code": "LOB-001"},
  {"file": "carta_antiga.jpg", "name": "Kuriboh"}
]
```

- `file`: nome do arquivo de imagem, relativo a esta pasta.
- `name`: nome **exato** da carta (como está no catálogo do YGOPRODeck).
- `set_code`: opcional — omita quando a foto não mostra um código legível.

## Como usar

```bash
# Contra o catálogo real (precisa de `yugioh-scanner init` primeiro):
python scripts/calibrate_thresholds.py

# Suíte de teste (critério de aceitação da Fase 9 — pula sozinha sem corpus):
pytest -m ocr tests/ocr/test_corpus_accuracy.py
```

O script roda OCR + matching uma vez por foto e varre combinações de
`CONFIDENCE_AUTO`/`CONFIDENCE_REVIEW`, reportando os limiares com zero
falso-positivo em auto-aceite e o maior recall — é esse relatório que
atualiza `docs/adr/0001-limiares-de-confianca.md` com os números medidos.
