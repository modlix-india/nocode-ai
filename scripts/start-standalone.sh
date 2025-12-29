#!/bin/bash
# Start the AI service in standalone mode (no config server, no eureka)
# Useful for testing without running nocode-saas services

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
echo "Nocode AI Service - Standalone Mode"
echo "=============================================="

# Service settings
export SERVICE_NAME="ai"
export SERVICE_PORT="${SERVICE_PORT:-5001}"

# Disable external services
export CONFIG_SERVER_ENABLED="false"
export EUREKA_ENABLED="false"

# You must set ANTHROPIC_API_KEY for standalone mode
if [ -z "$ANTHROPIC_API_KEY" ]; then
    echo ""
    echo "Error: ANTHROPIC_API_KEY is required in standalone mode"
    echo ""
    echo "Usage:"
    echo "  ANTHROPIC_API_KEY=sk-ant-xxx ./scripts/start-standalone.sh"
    echo ""
    exit 1
fi

# ChromaDB
export CHROMA_PERSIST_DIR="${CHROMA_PERSIST_DIR:-./data/chroma}"

# Embedding model
export EMBEDDING_MODEL="${EMBEDDING_MODEL:-local}"
export LOCAL_EMBEDDING_MODEL="${LOCAL_EMBEDDING_MODEL:-BAAI/bge-small-en-v1.5}"

# Document paths
export AICONTEXT_PATH="${AICONTEXT_PATH:-../nocode-ui/ui-app/aicontext}"

echo ""
echo "Configuration:"
echo "  Port: $SERVICE_PORT"
echo "  Config Server: DISABLED"
echo "  Eureka: DISABLED"
echo "  ChromaDB: $CHROMA_PERSIST_DIR"
echo "  Anthropic API Key: ****${ANTHROPIC_API_KEY: -8}"
echo ""
echo "Starting server on http://localhost:$SERVICE_PORT..."
echo "Press Ctrl+C to stop"
echo "=============================================="
echo ""

# Start the server with hot reload
uvicorn app.main:app --reload --host 0.0.0.0 --port "$SERVICE_PORT"

