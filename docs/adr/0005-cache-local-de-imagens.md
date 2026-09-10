# ADR 0005 — Cache local de imagens obrigatório; nunca hotlink ao YGOPRODeck

**Status:** aceito
**Data:** 2026-09-08 (Fase 1), implementado na Fase 8

## Contexto

A política de uso do YGOPRODeck (plano §0.2, verificada na documentação
oficial da API) proíbe hotlink direto das imagens e desaconselha baixar o
catálogo de imagens inteiro de uma vez (~300 MB a ~1,5 GB para artes que o
usuário nunca vai possuir). A UI precisa mostrar a arte de cada carta —
no dashboard, na revisão, na página de detalhe.

## Decisão

Cache preguiçoso (*lazy*) com prefetch opcional: a UI nunca aponta para
`images.ygoprodeck.com` diretamente — sempre para
`/api/v1/cards/{id}/image?size=small|full`, um endpoint nosso. Na primeira
vez, o endpoint baixa (respeitando o mesmo rate limiter da API,
`images/cache.py::ImageCache`), grava em
`data/images/{cards,cards_small}/{passcode}.jpg` e serve; depois disso,
serve sempre do disco, com `Cache-Control: public, max-age=31536000,
immutable`. Download concorrente da mesma imagem é serializado por um lock
em memória por `(tamanho, passcode)` — duas abas pedindo a mesma carta ao
mesmo tempo não disparam dois downloads.

## Consequências

- Positivo: verificado na Fase 8 — com o cache quente, uma segunda
  requisição pela mesma imagem não toca a rede (teste automatizado
  `test_second_request_never_touches_the_network`).
- Positivo: uso de disco proporcional à coleção real do usuário
  (~40 MB para 2.000 cartas), não ao catálogo inteiro.
- Negativo: a primeira visualização de cada carta é mais lenta (1 download).
  Mitigado por `sync --images` (prefetch só da coleção) quando o usuário
  quer garantir uso 100% offline antes de uma viagem, por exemplo.
- O endpoint de imagem só aceita `card_id` inteiro — nunca um caminho vindo
  do cliente (plano §21); o mesmo princípio foi reaplicado em
  `GET /api/v1/scan-images/{id}/file` na Fase 8, para servir a foto
  original ao lado da leitura na tela de revisão.
