# ADR 0003 — RapidOCR como motor de OCR padrão

**Status:** aceito
**Data:** 2026-09-08 (Fase 1)

## Contexto

O ambiente alvo é Windows, de um usuário que não necessariamente já tem
ferramentas de OCR instaladas. Motores de OCR variam muito em facilidade de
instalação: Tesseract exige um instalador de sistema separado e configurar
o `PATH`; PaddleOCR arrasta `paddlepaddle`, pesado e historicamente frágil
no Windows.

## Decisão

RapidOCR (modelos PP-OCR rodando em ONNXRuntime) é o provider padrão
(`OCR_PROVIDER=rapidocr`). Instala com `pip install -e ".[ocr]"`, sem
instalador de sistema. Precisão comparável ao PaddleOCR por usar o mesmo
modelo, sem a fragilidade da dependência `paddlepaddle`.

O acesso é sempre por trás de `ocr/base.py::OCRProvider` (`Protocol`) e
`ocr/registry.py` (factory por nome) — trocar de provider é mudar uma
variável de ambiente, nenhum outro módulo importa uma biblioteca de OCR
diretamente. `tesseract` é o alternativo já implementado; `paddle` e
`easyocr` estão na cascata do plano original (§6.3) mas **não foram
construídos** ainda (gap identificado na Fase 9, ver `PLAN.md` §19.5).

## Consequências

- Positivo: `pip install -e ".[ocr]"` sozinho já deixa o scanner funcional
  no Windows, sem passo manual de instalação de sistema.
- Positivo: trocar de provider (quando `paddle`/`easyocr` forem
  implementados, ou para usar `tesseract` hoje) é configuração, não código.
- Negativo: RapidOCR/ONNXRuntime não tem wheel para toda combinação de
  SO/arquitetura/versão de Python — ver seção de solução de problemas do
  `README.md`.
- Pendente: a cascata completa do §6.3 (múltiplos providers com fallback
  automático) não existe — hoje é um provider por vez, escolhido por
  configuração, não uma cascata que tenta o próximo se o atual falhar.
