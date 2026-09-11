# Corpus de calibração (Fase 9)

20 fotos reais (fornecidas pelo usuário em 2026-09-11, `imagens-test/` na
raiz do repo) — a maioria cartas em português, duas em inglês, uma em
japonês, todas fotografadas dentro de um álbum/binder de plástico. Ver
`docs/adr/0001-limiares-de-confianca.md` para a medição completa.

**Resultado medido: 40% de acerto top-1 do nome** (meta: ≥80%) — abaixo do
esperado. Causa dominante identificada: a transcrição do RapidOCR degrada
bastante nessas condições específicas (foto através do plástico do álbum,
não carta solta sobre fundo liso — o caso que `images/preprocess.py`
documenta como alvo). Um bug real de ordenação de texto foi encontrado e
corrigido no processo (`OCRResult.joined()` não ordenava por posição); o
ganho foi real mas pequeno (35%→40%) — não resolve a causa dominante.

**Isto não significa que a política de confiança é insegura**: em nenhuma
combinação de limiar testada uma leitura errada teria entrado sozinha na
coleção — as leituras ruins ficaram com confiança baixa o bastante para cair
em revisão manual. Ver o ADR para os números completos.

## Se for adicionar mais fotos

O corpus atual não tem nenhuma carta solta sobre fundo liso (o caso "fácil"
documentado) — seria o teste mais barato para confirmar a hipótese do
plástico do álbum. Do que já está, também faltam por cobrir explicitamente:

- carta solta, sem plástico, fundo liso (o caso de uso documentado — ainda
  não testado)
- carta com holografia/foil isolada (algumas fotos atuais têm, mas não
  isoladamente identificadas)
- pelo menos uma carta cujo nome é parecido com o de outra (testa o
  desempate por margem, plano §7.4) — não incluído ainda

Formatos aceitos: `.jpg`/`.jpeg`/`.png` (os mesmos do scanner — `.webp` não é
suportado; converta antes, como foi feito para `reino_toon.jpg`).

## `expected.json`

Um array com uma entrada por foto:

```json
[
  {"file": "blue_eyes_lob.jpg", "name": "Blue-Eyes White Dragon", "set_code": "LOB-001"},
  {"file": "carta_antiga.jpg", "name": "Kuriboh"}
]
```

- `file`: nome do arquivo de imagem, relativo a esta pasta.
- `name`: nome **exato e em inglês** da carta, como está em `Card.name` no
  catálogo (não o nome traduzido impresso na carta — o corpus atual tem
  cartas em português/japonês, e `name` é sempre o nome canônico que o
  matching deveria resolver, verificado por passcode contra o banco, não
  adivinhado por tradução).
- `set_code`: opcional — omita quando a foto não mostra um código legível
  **ou** quando o print regional não está catalogado pelo YGOPRODeck (achado
  real: nenhum dos prints `PT` deste corpus — `LEDD`, `SAST`, `L26D`, `RA05`,
  `SR06`, `SDCS` — está catalogado; só a série `OP##` tem prints PT no
  catálogo local. Testar `set_code` contra um código não catalogável mediria
  uma lacuna do catálogo, não do matching).

## Como usar

```bash
# Contra o catálogo real (precisa de `yugioh-scanner init` primeiro):
python scripts/calibrate_thresholds.py

# Suíte de teste (critério de aceitação da Fase 9):
pytest -m ocr tests/ocr/test_corpus_accuracy.py
```

O script roda OCR + matching uma vez por foto e varre combinações de
`CONFIDENCE_AUTO`/`CONFIDENCE_REVIEW`, reportando os limiares com zero
falso-positivo em auto-aceite e o maior recall — é esse relatório que
atualiza `docs/adr/0001-limiares-de-confianca.md` com os números medidos.
