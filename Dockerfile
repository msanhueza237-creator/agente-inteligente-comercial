FROM python:3.12-slim AS builder

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY app ./app
RUN pip install --no-cache-dir --prefix=/install .

FROM python:3.12-slim AS final

ENV PYTHONUNBUFFERED=1

RUN groupadd -r app && useradd -r -g app app

WORKDIR /app

COPY --from=builder /install /usr/local
COPY app ./app
COPY alembic.ini ./
COPY scripts ./scripts

RUN chown -R app:app /app
USER app

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
