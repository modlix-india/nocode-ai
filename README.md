# Nocode AI Service

Multi-agent AI service for generating nocode page definitions using Claude and RAG.

## Features

- **Multi-Agent Page Generation**: 7 specialized agents work together to create complete page definitions
- **SSE Streaming**: Real-time progress updates during generation
- **RAG System**: Retrieval-augmented generation using documentation and examples
- **Create/Modify/Enhance**: Support for new pages and modifications
- **Config Server Integration**: Fetches configuration from Spring Cloud Config Server
- **Eureka Integration**: Service discovery for microservices architecture

## Agents

| Agent         | Responsibility                                     |
| ------------- | -------------------------------------------------- |
| **Layout**    | Grid structure, responsive breakpoints, containers |
| **Component** | Component selection and properties                 |
| **Events**    | Event handlers and interactions                    |
| **Styles**    | Visual styling and theming                         |
| **Animation** | Animations and transitions                         |
| **Data**      | Data binding and store management                  |
| **Review**    | Validation and quality improvement                 |

## Local Development Setup

### 1. Create Virtual Environment

```bash
cd /Users/kirangrandhi/kiran/fincity/nocode-ai

# Create venv
python3 -m venv venv

# Activate
source venv/bin/activate
```

### 2. Install Dependencies

```bash
pip install -r requirements.txt
```

### 3. Start the Service

**Option A: With Config Server (recommended if nocode-saas services are running)**

```bash
# Make sure config server is running at localhost:8888
# Uses 'default' profile by default
uvicorn app.main:app --reload --port 5001

# Or specify a different profile (dev, stage, prod)
SPRING_PROFILE=dev uvicorn app.main:app --reload --port 5001
```

The service fetches config from: `http://localhost:8888/ai/{profile}`

**Option B: Without Config Server (standalone)**

```bash
# Disable config server and set API key directly
CONFIG_SERVER_ENABLED=false \
EUREKA_ENABLED=false \
ANTHROPIC_API_KEY=your-key-here \
uvicorn app.main:app --reload --port 5001
```

### 4. Ingest Documents (First Time)

```bash
python scripts/ingest.py
```

## Configuration

### Config Server Integration

The service fetches configuration from Spring Cloud Config Server at startup:

| Config Server Key            | Environment Variable   | Description          |
| ---------------------------- | ---------------------- | -------------------- |
| `ai.security.url`            | `SECURITY_SERVICE_URL` | Security service URL |
| `ai.secrets.anthropicAPIKey` | `ANTHROPIC_API_KEY`    | Anthropic API key    |

**Priority**: Environment variables > Config Server values > Default values

### Environment Variables

```bash
# Service
SERVICE_NAME=ai
SERVICE_PORT=5001

# Config Server
CONFIG_SERVER_URL=http://localhost:8888
CONFIG_SERVER_ENABLED=true
SPRING_PROFILE=default  # Options: default, dev, stage, prod

# Eureka
EUREKA_ENABLED=true
EUREKA_SERVER=http://localhost:9999/eureka/

# Embeddings
EMBEDDING_MODEL=local
LOCAL_EMBEDDING_MODEL=BAAI/bge-small-en-v1.5

# ChromaDB
CHROMA_PERSIST_DIR=./data/chroma

# Document paths
AICONTEXT_PATH=../nocode-ui/ui-app/aicontext
```

## API Endpoints

### Generate Page (SSE Streaming)

```bash
curl -N -X POST http://localhost:5001/api/ai/agent/page \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <token>" \
  -d '{
    "instruction": "Create a login page with email and password fields",
    "options": { "mode": "create" }
  }'
```

### Generate Page (Synchronous)

```bash
curl -X POST http://localhost:5001/api/ai/agent/page/sync \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <token>" \
  -d '{
    "instruction": "Create a login page",
    "options": { "mode": "create" }
  }'
```

### Query Documentation

```bash
curl -X POST http://localhost:5001/api/ai/query \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <token>" \
  -d '{
    "query": "How do I create a form with validation?",
    "topK": 5
  }'
```

## Project Structure

```
nocode-ai/
├── app/
│   ├── main.py                    # FastAPI application
│   ├── config.py                  # Configuration
│   ├── api/routes/
│   │   ├── health.py              # Health check
│   │   ├── agent.py               # Page generation with SSE
│   │   └── query.py               # RAG query
│   ├── agents/
│   │   ├── page_agent.py          # Page Agent (orchestrator)
│   │   └── ...                    # Sub-agents
│   ├── rag/
│   │   ├── engine.py              # RAG engine
│   │   └── ...
│   ├── services/
│   │   ├── config_server.py       # Config Server client
│   │   ├── eureka.py              # Eureka registration
│   │   └── security.py            # Token validation
│   └── streaming/
│       └── events.py              # SSE events
├── scripts/
│   └── ingest.py                  # Document ingestion
├── definitions/                   # Example definitions for RAG
├── data/chroma/                   # ChromaDB persistence
├── Dockerfile                     # For cloud deployments
├── docker-compose.yml             # For cloud deployments
├── requirements.txt
└── README.md
```

## License

Proprietary - Modlix
