FROM python:3.12-slim
# Системные библиотеки для WeasyPrint, шрифт с кириллицей и утилиты PostgreSQL для бэкапов из панели
RUN apt-get update && apt-get install -y --no-install-recommends \
      libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz-subset0 fonts-dejavu-core postgresql-client \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml ./
COPY shturman ./shturman
COPY landing ./landing
COPY scripts ./scripts
RUN pip install --no-cache-dir . \
    && useradd --uid 1000 --create-home --shell /usr/sbin/nologin app \
    && mkdir -p /app/backups && chown app:app /app/backups
# Приложение работает не от root: при взломе процесса у злоумышленника не будет прав на систему контейнера.
USER app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
EXPOSE 8000
CMD ["uvicorn", "shturman.api.app:get_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
