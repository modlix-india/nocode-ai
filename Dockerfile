# Production image with HuggingFace local embeddings
FROM python:3.11-slim

WORKDIR /app

# Install runtime dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY app/ ./app/
COPY scripts/ ./scripts/

# Create data directory for ChromaDB
RUN mkdir -p /app/data/chroma

# Environment defaults
ENV SERVICE_NAME=ai
ENV SERVICE_PORT=5001
ENV CHROMA_PERSIST_DIR=/app/data/chroma

# Local HuggingFace embeddings (no API key needed)
ENV EMBEDDING_MODEL=local
ENV LOCAL_EMBEDDING_MODEL=BAAI/bge-small-en-v1.5

# Config Server settings
ENV CLOUD_CONFIG_SERVER=localhost
ENV CONFIG_SERVER_PORT=8888
ENV CONFIG_SERVER_ENABLED=true
ENV SPRING_PROFILES_ACTIVE=default

# Eureka settings
ENV EUREKA_ENABLED=true
ENV EUREKA_SERVER=http://localhost:9999/eureka/

# Gunicorn settings for production
# Workers = (2 * CPU cores) + 1, default 4 for most deployments
# Timeout 300s for long-running AI generation requests
# Keep-alive 300s for SSE connections
ENV GUNICORN_WORKERS=4
ENV GUNICORN_TIMEOUT=300
ENV GUNICORN_KEEPALIVE=300

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD curl -f http://localhost:5001/health || exit 1

EXPOSE 5001

# Use Gunicorn with Uvicorn workers for production
# - Multiple workers for better concurrency
# - Uvicorn workers for async support
# - Long timeout for AI generation
CMD ["gunicorn", "app.main:app", \
    "--workers", "4", \
    "--worker-class", "uvicorn.workers.UvicornWorker", \
    "--bind", "0.0.0.0:5001", \
    "--timeout", "300", \
    "--keep-alive", "300", \
    "--access-logfile", "-", \
    "--error-logfile", "-"]
