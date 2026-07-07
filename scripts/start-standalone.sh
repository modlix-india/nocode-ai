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

# Standalone mode — route API calls through the local webpack dev server.
# The webpack proxy forwards /api/** to apps.dev.modlix.com.
# X-Path-Prefix header (extracted from the incoming URL by webpack) is used
# to dynamically prepend /{appCode}/{clientCode}/page to outgoing API paths.
export STANDALONE_MODE="true"
export GATEWAY_URL="${GATEWAY_URL:-https://dev.adzump.ai}"
export SECURITY_SERVICE_URL="${SECURITY_SERVICE_URL:-https://dev.adzump.ai}"
export FILES_SERVICE_URL="${FILES_SERVICE_URL:-https://dev.adzump.ai}"

# Default to Anthropic for AppBuilder in standalone mode (override with APPBUILDER_PROVIDER env var)
export LLM_PROVIDER="${LLM_PROVIDER:-anthropic}"
export APPBUILDER_PROVIDER="${APPBUILDER_PROVIDER:-deepseek}"

# Load API keys from ~/.nocode-ai/variables.sh (if present)
if [ -f "$HOME/.nocode-ai/variables.sh" ]; then
    source "$HOME/.nocode-ai/variables.sh"
else
    echo ""
    echo "Warning: ~/.nocode-ai/variables.sh not found."
    echo ""
    echo "Create it with your API keys:"
    echo "  mkdir -p ~/.nocode-ai"
    echo "  cat > ~/.nocode-ai/variables.sh << 'EOF'"
    echo '  export ANTHROPIC_API_KEY="sk-ant-..."'
    echo '  export DEEPSEEK_API_KEY="sk-..."'
    echo '  export OPENAI_API_KEY="sk-..."'
    echo '  export GOOGLE_API_KEY="..."'
    echo "  EOF"
    echo ""
    echo "Or export keys directly:"
    echo "  export ANTHROPIC_API_KEY=sk-ant-xxx && ./scripts/start-standalone.sh"
    echo ""
fi

# Require at least one API key
if [ -z "$ANTHROPIC_API_KEY" ] && [ -z "$DEEPSEEK_API_KEY" ] && [ -z "$OPENAI_API_KEY" ]; then
    echo "Error: At least one API key is required."
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
echo "  Anthropic API Key: ${ANTHROPIC_API_KEY:+****${ANTHROPIC_API_KEY: -8}}${ANTHROPIC_API_KEY:-NOT SET}"
echo "  DeepSeek API Key:  ${DEEPSEEK_API_KEY:+****${DEEPSEEK_API_KEY: -8}}${DEEPSEEK_API_KEY:-NOT SET}"
echo "  OpenAI API Key:    ${OPENAI_API_KEY:+****${OPENAI_API_KEY: -8}}${OPENAI_API_KEY:-NOT SET}"
echo "  Google API Key:    ${GOOGLE_API_KEY:+****${GOOGLE_API_KEY: -8}}${GOOGLE_API_KEY:-NOT SET}"
echo ""
echo "Starting server on http://localhost:$SERVICE_PORT..."
echo "Press Ctrl+C to stop"
echo "=============================================="
echo ""

# Start the server with hot reload
uvicorn app.main:app --reload --host 0.0.0.0 --port "$SERVICE_PORT"

