# ADR 0007 — Sem autenticação nesta versão

**Status:** aceito
**Data:** 2026-09-08 (Fase 1), reafirmado na Fase 8

## Contexto

App de uso pessoal, single-user, rodando na máquina do próprio usuário.
Autenticação real (contas, sessões, senha ou token) é trabalho não-trivial
que não protege nada relevante enquanto o único acesso é `localhost`.

## Decisão

Nenhuma autenticação nesta versão. A mitigação é de rede, não de
aplicação: bind em `127.0.0.1` por padrão (`web_host` em `config.py`).
Trocar para `0.0.0.0` (ou qualquer host não-loopback) exige uma flag
explícita **e** imprime um aviso — `yugioh-scanner web --host 0.0.0.0`
avisa claramente que a aplicação não tem autenticação (`cli/web_cmd.py`).

O código já isola o acesso a dados atrás dos serviços de aplicação
(`services/*`) — nenhuma rota HTML ou de API toca o banco diretamente. Uma
dependência `get_current_user` do FastAPI, quando/se autenticação for
necessária, se encaixa na camada de rotas sem reescrever serviço nenhum.

## Consequências

- Positivo: nenhuma complexidade de sessão/senha/token para um app que
  nunca sai do laptop do usuário.
- Positivo: o caminho de adição está mapeado e não exige retrabalho da
  camada de serviços.
- Risco aceito, documentado: **CSRF não está implementado** nos formulários
  HTMX (plano §21 já previa isso como "barato agora, obrigatório quando
  houver auth" — condição ainda não satisfeita). Se a rede binder em algo
  além de loopback algum dia, isto precisa ser revisitado *antes*, não
  depois.
