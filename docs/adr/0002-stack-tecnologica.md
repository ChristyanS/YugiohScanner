# ADR 0002 — Stack: FastAPI + Jinja2/HTMX + SQLAlchemy 2.0 + Typer + RapidOCR

**Status:** aceito
**Data:** 2026-09-08 (Fase 1)

## Contexto

App local e pessoal para um único usuário, sem necessidade de escala
horizontal, autenticação multiusuário ou API pública versionada. Precisa de
duas interfaces (terminal e navegador) compartilhando a mesma lógica de
negócio, um banco embutido sem servidor separado, e um pipeline de OCR que
rode bem no Windows sem dependências de sistema complexas.

## Decisão

- **Backend web: FastAPI + Uvicorn.** Tipagem nativa com Pydantic (o projeto
  inteiro já é tipado), OpenAPI de graça, serve Jinja2 tão bem quanto JSON.
  Django traz ORM/admin/auth que não seriam usados (peso morto); Flask
  exigiria montar validação e schemas à mão.
- **Frontend: Jinja2 + HTMX + Pico.css, sem build step.** App pessoal e
  local: zero `node_modules`, zero API JSON duplicada só para o frontend.
  HTMX cobre exatamente o necessário (tabela com filtro/ordenação, progresso
  via SSE, edição inline). React/Vue exigiriam um segundo runtime e bundler
  sem retorno proporcional aqui.
- **ORM: SQLAlchemy 2.0** (declarativo tipado, `Mapped[]`) **+ Alembic.**
  2.0 tem tipagem estática de primeira classe. SQLModel funde modelo de
  persistência e schema de API (acopla banco e HTTP); preferimos Pydantic
  separado para schemas/DTOs. Alembic é obrigatório porque o schema mudaria
  entre fases.
- **CLI: Typer + Rich.** Tipagem consistente com o resto do projeto, ajuda
  automática, tabelas legíveis sem código extra.
- **OCR padrão: RapidOCR** (ONNXRuntime). `pip install` puro, sem
  dependência de sistema no Windows — ver [ADR 0003](0003-rapidocr-como-padrao.md).
- **Banco: SQLite + FTS5**, WAL. Sem servidor separado para instalar; FTS5
  cobre a busca por nome sem precisar de Elasticsearch/Postgres.

## Consequências

- Positivo: instalação é `pip install`, sem Docker/serviços externos
  obrigatórios; um único processo Python roda tudo.
- Positivo: HTMX + Jinja2 manteve a Fase 8 pequena — nenhuma camada de
  build, nenhum `package.json`.
- Negativo: SQLite é single-writer — decisão que se propaga por todo o
  design do scanner (workers nunca tocam o banco, ver `architecture.md`).
- Negativo: sem autenticação nativa de framework — ver
  [ADR 0007](0007-sem-autenticacao.md).
