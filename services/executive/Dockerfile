# Build context is the repository root:
#   docker build -f services/executive/Dockerfile .
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app:/app/shared

WORKDIR /app

COPY services/executive/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Shared contract, role definitions and company context.
COPY shared/ ./shared/
COPY agents/ ./agents/
COPY context/ ./context/
COPY services/executive/main.py ./main.py

RUN useradd --create-home --uid 1000 appuser && chown -R appuser:appuser /app
USER appuser

# Cloud Run injects PORT; EXEC_ROLE selects which executive this instance is.
ENV PORT=8080
EXPOSE 8080

CMD exec uvicorn main:app --host 0.0.0.0 --port ${PORT}
