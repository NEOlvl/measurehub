# API, Celery worker and one-shot migration share this image (different commands).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY pyproject.toml README.md alembic.ini ./
COPY src ./src
COPY simulator ./simulator
COPY migrations ./migrations
RUN pip install .

RUN useradd --create-home app
USER app

EXPOSE 8000
CMD ["uvicorn", "--factory", "measurehub.api.app:app_factory", "--host", "0.0.0.0", "--port", "8000"]
