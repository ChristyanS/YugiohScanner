# Architecture Decision Records

Uma decisão por arquivo: contexto, decisão, consequências. Números não são
reutilizados; uma decisão revista ganha uma entrada nova que referencia a
antiga, a não ser que a instrução no próprio ADR diga para atualizá-lo no
lugar (caso do 0001, que será atualizado com os números reais assim que a
calibração rodar).

| ADR | Título |
|---|---|
| [0001](0001-limiares-de-confianca.md) | Limiares de confiança: julgamento de engenharia, calibração pendente |
| [0002](0002-stack-tecnologica.md) | Stack: FastAPI + Jinja/HTMX + SQLAlchemy 2.0 + Typer + RapidOCR |
| [0003](0003-rapidocr-como-padrao.md) | RapidOCR como motor de OCR padrão |
| [0004](0004-claude-vision-fallback-opcional.md) | Claude Vision como fallback opcional (Fase 10, não implementado) |
| [0005](0005-cache-local-de-imagens.md) | Cache local de imagens obrigatório; nunca hotlink ao YGOPRODeck |
| [0006](0006-hexagonal-seletivo.md) | Hexagonal aplicado só em OCR e exportadores |
| [0007](0007-sem-autenticacao.md) | Sem autenticação nesta versão |
| [0008](0008-progresso-de-scan-via-run-id.md) | Progresso de scan via `run_id` opaco, não `job_id` |

Ver [`../architecture.md`](../architecture.md) para a visão geral atual do
sistema, e [`../PLAN.md`](../PLAN.md) para o plano técnico original completo.
