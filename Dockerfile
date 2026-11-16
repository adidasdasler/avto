FROM python:3.12-slim
# Системные библиотеки для WeasyPrint и шрифт с кириллицей
RUN apt-get update && apt-get install -y --no-install-recommends \
      libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz-subset0 fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml ./
COPY shturman ./shturman
COPY landing ./landing
COPY scripts ./scripts
RUN pip install --no-cache-dir .
ENV PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["uvicorn", "shturman.api.app:get_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
