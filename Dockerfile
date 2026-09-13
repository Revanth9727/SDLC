FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY docs ./docs
COPY README.md ./
RUN useradd --create-home --uid 10001 agent \
    && mkdir -p /tmp/agentic-workspaces \
    && chown -R agent:agent /app /tmp/agentic-workspaces
USER agent
EXPOSE 8000
CMD ["sh", "-c", "python -m app.db.init_db && exec uvicorn app.main:app --host 0.0.0.0 --port 8000"]
