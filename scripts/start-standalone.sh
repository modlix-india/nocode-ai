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

# Route API calls through remote dev gateway (override with env vars if needed)
export GATEWAY_URL="${GATEWAY_URL:-https://apps.dev.modlix.com}"
export SECURITY_SERVICE_URL="${SECURITY_SERVICE_URL:-https://apps.dev.modlix.com}"
export FILES_SERVICE_URL="${FILES_SERVICE_URL:-https://apps.dev.modlix.com}"

# Default to Anthropic for AppBuilder in standalone mode (override with APPBUILDER_PROVIDER env var)
export LLM_PROVIDER="${LLM_PROVIDER:-anthropic}"
export APPBUILDER_PROVIDER="${APPBUILDER_PROVIDER:-deepseek}"

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

echo ""
echo "Configuration:"
echo "  Port: $SERVICE_PORT"
echo "  Config Server: DISABLED"
echo "  Eureka: DISABLED"
echo "  Gateway URL: $GATEWAY_URL"
echo "  Security URL: $SECURITY_SERVICE_URL"
echo "  LLM Provider: $LLM_PROVIDER"
echo "  AppBuilder Provider: $APPBUILDER_PROVIDER"
echo "  Anthropic API Key: ****${ANTHROPIC_API_KEY: -8}"
echo ""
echo "Starting server on http://localhost:$SERVICE_PORT..."
echo "Press Ctrl+C to stop"
echo "=============================================="
echo ""

# Start the server with hot reload
uvicorn app.main:app --reload --host 0.0.0.0 --port "$SERVICE_PORT"

