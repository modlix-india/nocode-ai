#!/bin/bash
# Start the AI service locally for development
# Connects to local nocode-saas services (config server, eureka, security)

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_DIR"

# Check if venv exists
if [ ! -d "venv" ]; then
    echo "Error: Virtual environment not found!"
    echo "Run: python3 -m venv venv && source venv/bin/activate && pip install -r requirements.txt"
    exit 1
fi

# Activate venv
source venv/bin/activate

echo "=============================================="
echo "Nocode AI Service - Local Development"
echo "=============================================="

# Default settings for local development
export SERVICE_NAME="${SERVICE_NAME:-ai}"
export SERVICE_PORT="${SERVICE_PORT:-5001}"

# Config Server settings
export CLOUD_CONFIG_SERVER="${CLOUD_CONFIG_SERVER:-localhost}"
export CONFIG_SERVER_PORT="${CONFIG_SERVER_PORT:-8888}"
export CONFIG_SERVER_ENABLED="${CONFIG_SERVER_ENABLED:-true}"
export SPRING_PROFILES_ACTIVE="${SPRING_PROFILES_ACTIVE:-default}"

# Eureka settings
export EUREKA_ENABLED="${EUREKA_ENABLED:-true}"
export EUREKA_SERVER="${EUREKA_SERVER:-http://localhost:9999/eureka/}"
export EUREKA_INSTANCE_HOST="${EUREKA_INSTANCE_HOST:-localhost}"

# ChromaDB
export CHROMA_PERSIST_DIR="${CHROMA_PERSIST_DIR:-./data/chroma}"

# Embedding model
export EMBEDDING_MODEL="${EMBEDDING_MODEL:-local}"
export LOCAL_EMBEDDING_MODEL="${LOCAL_EMBEDDING_MODEL:-BAAI/bge-small-en-v1.5}"

# Document paths
export AICONTEXT_PATH="${AICONTEXT_PATH:-../nocode-ui/ui-app/aicontext}"
export APP_DEFINITIONS_PATH="${APP_DEFINITIONS_PATH:-./definitions/app defs}"
export SITE_DEFINITIONS_PATH="${SITE_DEFINITIONS_PATH:-./definitions/site defs}"

echo ""
echo "Configuration:"
echo "  Port: $SERVICE_PORT"
echo "  Config Server: http://$CLOUD_CONFIG_SERVER:$CONFIG_SERVER_PORT"
echo "  Profile: $SPRING_PROFILES_ACTIVE"
echo "  Eureka: $EUREKA_SERVER (enabled: $EUREKA_ENABLED)"
echo "  ChromaDB: $CHROMA_PERSIST_DIR"
echo ""

# Check if config server is running (optional)
if [ "$CONFIG_SERVER_ENABLED" = "true" ]; then
    if curl -s "http://$CLOUD_CONFIG_SERVER:$CONFIG_SERVER_PORT/actuator/health" > /dev/null 2>&1; then
        echo "✓ Config server is running"
    else
        echo "⚠ Config server not reachable at http://$CLOUD_CONFIG_SERVER:$CONFIG_SERVER_PORT"
        echo "  Service will use environment variables for configuration"
    fi
fi

echo ""
echo "Starting server on http://localhost:$SERVICE_PORT..."
echo "Press Ctrl+C to stop"
echo "=============================================="
echo ""

# Start the server with hot reload
uvicorn app.main:app --reload --host 0.0.0.0 --port "$SERVICE_PORT"

