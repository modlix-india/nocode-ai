# Nocode AI Service

Agentic AI service for building no-code applications using Claude with 60+ specialized tools and RAG.

## At a Glance

| | |
|---|---|
| **Framework** | FastAPI (async), Python 3.9+ |
| **LLM** | Claude (Anthropic) — with OpenAI fallback |
| **Architecture** | Single-agent, multi-tool loop (Claude Code-style) |
| **Tools** | 60+ tools — pages, components, events, styles, functions, entities, versions |
| **Streaming** | Server-Sent Events (SSE) for real-time output |
| **RAG** | ChromaDB + FastEmbed (local embeddings, no API key needed) |
| **Session Tracking** | MySQL (aiomysql) — conversations, token usage, tool calls |
| **Rate Limiting** | Redis (optional, graceful degradation) |
| **Service Discovery** | Eureka + Spring Cloud Config integration |
| **Prompt Caching** | ~90% token savings on repeated system prompts (Anthropic) |
| **Port** | `5001` |

## Quick Start

```bash
# 1. Setup
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# 2. Configure (.env)
ANTHROPIC_API_KEY=sk-ant-your-key
CONFIG_SERVER_ENABLED=false
EUREKA_ENABLED=false

# 3. Ingest docs (first time only)
python scripts/ingest.py

# 4. Run
uvicorn app.main:app --reload --port 5001
```

Verify: `curl http://localhost:5001/health`

## How It Works

```
User message → Build system prompt (static + dynamic context)
                    ↓
              Call LLM with tools → Stream text via SSE
                    ↓
              tool_use? → Execute tool → Emit result → Loop back
                    ↓
              end_turn? → Done → Emit session stats
```

Large objects (30K+ JSON pages) are **never shown to the agent**. The agent sees compact tree structures and operates at the component level. A Python executor handles read-modify-write internally.

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/ai/appbuilder/chat` | SSE streaming agent chat |
| `GET` | `/api/ai/appbuilder/sessions` | List sessions (paginated) |
| `GET` | `/api/ai/appbuilder/sessions/{id}` | Session detail + history |
| `PATCH` | `/api/ai/appbuilder/sessions/{id}` | Rename session |
| `DELETE` | `/api/ai/appbuilder/sessions/{id}` | Delete session |
| `POST` | `/api/ai/query` | RAG documentation query |
| `GET` | `/health` | Health check |
| `GET` | `/health/detailed` | Detailed component health |

### Example: Chat

```bash
curl -N -X POST http://localhost:5001/api/ai/appbuilder/chat \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <token>" \
  -H "clientCode: SYSTEM" \
  -H "appCode: appbuilder" \
  -d '{"message": "Create a login page with email and password fields"}'
```

### SSE Event Types

`text` | `tool_start` | `tool_result` | `error` | `done` | `keepalive`

## Tool Categories

| Category | Count | Examples |
|----------|-------|---------|
| **Page** | 6 | `create_page`, `read_page_structure`, `delete_page` |
| **Component** | 8 | `add_component`, `update_component`, `move_component` |
| **Batch** | 6 | Bulk component operations |
| **Event** | 4 | `write_event_function`, `read_event_function` |
| **Application** | 7 | App CRUD, export, import |
| **Style** | 4 | Theme + style CRUD |
| **Function** | 6 | Function + schema CRUD, search builtins |
| **Entity** | 10 | Connection, workflow, template, filler, URI path |
| **Version** | 4 | Version management |

## Project Structure

```
nocode-ai/
├── app/
│   ├── main.py                  # FastAPI entry + lifespan
│   ├── config.py                # Pydantic Settings
│   ├── core/                    # Shared agentic framework
│   │   ├── agent.py             #   BaseAgent (tool-use loop)
│   │   ├── context.py           #   System prompt builder
│   │   ├── session.py           #   Session model
│   │   ├── streaming.py         #   SSE protocol
│   │   └── tools/               #   ToolDefinition, SaasClient
│   ├── agents/appbuilder/       # AppBuilder agent
│   │   ├── agent.py             #   AppBuilderAgent
│   │   ├── router.py            #   Chat + session endpoints
│   │   ├── AGENT.md             #   Agent specification
│   │   └── tools/               #   60+ tool implementations
│   ├── services/                # LLM, Eureka, Config, Security, Sessions, Redis
│   ├── api/                     # Routes + request models
│   ├── rag/                     # ChromaDB + FastEmbed
│   ├── db/                      # MySQL models, connections, migrations
│   └── middleware/              # Rate limiting
├── migrations/                  # SQL migrations (V1–V5)
├── scripts/                     # start-local.sh, start-production.sh
├── data/chroma/                 # Vector store (gitignored)
├── docs/                        # Documentation
├── Dockerfile
└── requirements.txt
```

## Configuration

**Priority**: Environment variables > Config Server > Defaults

| Variable | Default | Description |
|----------|---------|-------------|
| `LLM_PROVIDER` | `anthropic` | `anthropic` or `openai` |
| `ANTHROPIC_API_KEY` | — | Required for Anthropic provider |
| `AGENT_MODEL_TIER` | `balanced` | `fast` (Haiku) or `balanced` (Sonnet) |
| `MAX_AGENT_TURNS` | `50` | Max tool-use iterations |
| `PROMPT_CACHING_ENABLED` | `true` | Anthropic prompt caching |
| `CONFIG_SERVER_ENABLED` | `true` | Fetch from Spring Cloud Config |
| `EUREKA_ENABLED` | `true` | Register with Eureka |
| `REDIS_URL` | — | Optional, enables rate limiting |
| `MYSQL_URL` | — | Optional, enables session persistence |

## Running with Full Stack

```bash
# With Config Server + Eureka (nocode-saas running)
uvicorn app.main:app --reload --port 5001

# Specify profile
SPRING_PROFILE=dev uvicorn app.main:app --reload --port 5001
```

## Docker

```bash
docker build -t nocode-ai .
docker run -p 5001:5001 -e ANTHROPIC_API_KEY=sk-ant-... -e CONFIG_SERVER_ENABLED=false -e EUREKA_ENABLED=false nocode-ai
```

Production uses Gunicorn + UvicornWorker with 4 workers (`scripts/start-production.sh`).

## Contributing

See [docs/CONTRIBUTING.md](docs/CONTRIBUTING.md).

## License

Proprietary - Modlix
