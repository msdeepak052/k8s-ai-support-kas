# Multi-stage Dockerfile for k8s-ai-support
# Stage 1: Builder — installs dependencies
# Stage 2: Runtime — minimal image with only what's needed

# ─────────────────────────── Build Stage ────────────────────────────────────
FROM python:3.11-slim as builder

# Install system deps for building Python packages
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

# Install Poetry
ENV POETRY_VERSION=1.8.3
ENV POETRY_HOME=/opt/poetry
ENV POETRY_VENV=/opt/poetry-venv
ENV POETRY_CACHE_DIR=/opt/.cache

RUN python3 -m venv $POETRY_VENV \
    && $POETRY_VENV/bin/pip install --upgrade pip \
    && $POETRY_VENV/bin/pip install poetry==${POETRY_VERSION}

ENV PATH="${POETRY_VENV}/bin:${PATH}"

WORKDIR /app

# Copy dependency files first (layer caching)
COPY pyproject.toml poetry.lock* ./

# Install ALL dependencies (including all extras) for a complete image
RUN poetry config virtualenvs.create false \
    && poetry install --no-root --extras "all" --no-interaction --no-ansi

# Install kubectl
RUN curl -LO "https://dl.k8s.io/release/$(curl -L -s https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl" \
    && chmod +x kubectl \
    && mv kubectl /usr/local/bin/kubectl

# Copy source code
COPY src/ ./src/
COPY README.md ./

# Install the package itself
RUN poetry install --no-interaction --no-ansi


# ─────────────────────────── Runtime Stage ──────────────────────────────────
FROM python:3.11-slim as runtime

# Labels
LABEL maintainer="k8s-ai-support"
LABEL version="1.0.0"
LABEL description="AI-powered Kubernetes troubleshooting agent"

# Security: run as non-root
RUN groupadd --gid 1001 k8sai \
    && useradd --uid 1001 --gid k8sai --shell /bin/bash --create-home k8sai

# Copy kubectl from builder
COPY --from=builder /usr/local/bin/kubectl /usr/local/bin/kubectl

# Copy Python environment from builder
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy application
COPY --from=builder /app/src /app/src
COPY --from=builder /app/README.md /app/README.md

WORKDIR /app

# Pre-create cache directory with correct permissions
RUN mkdir -p /home/k8sai/.cache/k8s-ai/rag/chroma \
    && chown -R k8sai:k8sai /home/k8sai/.cache

USER k8sai

# Environment defaults (override at runtime)
ENV K8S_AI_PROVIDER=openai
ENV K8S_AI_MODEL=gpt-4o-mini
ENV K8S_AI_LOG_LEVEL=INFO
ENV K8S_AI_LOG_FORMAT=json
ENV K8S_AI_TOKEN_BUDGET=8000
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD python -c "from src.config.settings import get_settings; get_settings(); print('OK')" || exit 1

# Expose no ports by default (stdio-based MCP server)
# Use --network=host when kubectl needs cluster access

ENTRYPOINT ["python", "-m", "src.cli.main"]
CMD ["--help"]
