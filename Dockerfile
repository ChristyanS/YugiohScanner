# Imagem de conveniência para rodar a interface Web em um container.
#
# NÃO VERIFICADA COM `docker build`/`docker run` — o ambiente onde este
# Dockerfile foi escrito não tem Docker disponível. O que FOI verificado sem
# Docker (venv limpo, só com os arquivos que este Dockerfile copia): o
# `pip install -e ".[ocr,web]"` completa, `yugioh-scanner --help`/`--version`
# funcionam, e `yugioh-scanner db upgrade` roda a migração do zero. O que
# fica sem verificar é especificamente a camada de container em si (imagem
# base, `apt-get`, ENTRYPOINT/CMD, volume, rede) — revise antes de depender
# dela (docs/PLAN.md Fase 11: "docker build + docker run funcionando" é o
# critério de aceitação que fica pendente até alguém rodar isto de verdade).
#
# Uso:
#   docker build -t yugioh-scanner .
#   docker run -d --name yugioh-scanner -p 8000:8000 -v yugioh-data:/app/data yugioh-scanner init
#   docker run -d --name yugioh-scanner -p 8000:8000 -v yugioh-data:/app/data yugioh-scanner web --host 0.0.0.0
#
# `--host 0.0.0.0` é necessário dentro do container (sem isso a app não é
# alcançável fora dele) — mas a aplicação continua sem autenticação
# (docs/adr/0007-sem-autenticacao.md). O isolamento de rede aqui é o mapeamento
# de porta do Docker, não a aplicação: não publique a porta num host
# multiusuário ou na rede pública sem entender essa limitação.

FROM python:3.10-slim

# libgomp1: onnxruntime (RapidOCR) usa OpenMP para paralelismo de inferência.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src/ ./src/
COPY migrations/ ./migrations/
COPY alembic.ini ./

RUN pip install --no-cache-dir -e ".[ocr,web]"

ENV YGS_DATA_PATH=/app/data
VOLUME ["/app/data"]
EXPOSE 8000

ENTRYPOINT ["yugioh-scanner"]
CMD ["web", "--host", "0.0.0.0"]
