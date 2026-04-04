FROM python:3.10-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml .
COPY README.md .
COPY wactorz/ ./wactorz/
COPY static/ ./static/
COPY monitor.html ./monitor.html
COPY scripts/ ./scripts/

RUN pip install --no-cache-dir ".[all]"

RUN mkdir -p /app/state

ENV INTERFACE=rest

EXPOSE 8000 8888

CMD ["wactorz"]