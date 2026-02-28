# CLAUDE.md — nocode-ai

## Overview

AI agent monorepo for the **Modlix no-code/low-code platform**. Contains shared agentic infrastructure and domain-specific agent implementations. Part of the Fincity polyglot monorepo alongside `nocode-saas` (Java backend), `nocode-ui` (React frontend), and `nocode-kirun` (runtime engine).

## Architecture

```
app/
├── core/              Shared agentic framework (agent loop, SSE, sessions, tools)
├── agents/{name}/     Agent implementations (one folder per agent type)
├── services/          Shared services (LLM provider, Eureka, Redis, Config Server)
├── api/               Shared API infrastructure (health, RAG query)
├── rag/               Shared RAG engine (ChromaDB + FastEmbed)
├── db/                MySQL session tracking
├── middleware/         Rate limiting, request deduplication
└── config.py          Settings (env vars + Config Server + defaults)
```

### Core Framework (`app/core/`)

```
app/core/
├── agent.py           BaseAgent — tool-use agentic loop (call LLM → stream → execute tools → loop)
├── streaming.py       SSE protocol (AgentEventType, AgentEvent, AgentEventStream)
├── session.py         BaseSession — wraps session_manager + context_manager
├── context.py         BaseContext — system prompt builder with Anthropic prompt caching
└── tools/
    ├── base.py        ToolDefinition, ToolResult, ToolParameter + to_anthropic_tool()
    └── http_client.py SaasClient — async httpx client for Gateway API calls
```

### Agent Pattern

Each agent in `app/agents/` follows this structure:

```
app/agents/{agent_name}/
├── __init__.py
├── agent.py           Extends core.agent.BaseAgent — configures model + system prompt
├── context.py         Builds the system prompt (loads domain docs, injects RAG context)
├── catalog.py         Dynamic component catalog (fetched from CDN at startup)
├── router.py          FastAPI router (POST /api/ai/{agent_name}/chat)
└── tools/             Domain-specific tool definitions
    ├── __init__.py
    ├── registry.py    All tool definitions registered here (exports ALL_TOOLS)
    ├── _executor.py   Shared page read-modify-write executor
    ├── _shared.py     Shared SaasClient singleton + helper utilities
    └── *.py           One file per tool category (page, component, event, application, style, function, entity)
```

To add a new agent: create the folder, extend BaseAgent, define tools, add router, register in main.py.

## Current Agent: AppBuilder

The `appbuilder` agent builds entire no-code applications through multi-turn conversation. See `app/agents/appbuilder/AGENT.md` for full specification.

**Tool categories:**
- `page_tools.py` — list_pages, create_page, delete_page, read_page_structure, read/update_page_properties
- `component_tools.py` — add_component, update_component, read_component, remove_component, move_component
- `event_tools.py` — write_event_function, read_event_function, list_event_functions
- `application_tools.py` — create/read/update/list/delete_application, export/import_application
- `style_tools.py` — theme + style CRUD
- `function_tools.py` — function + schema CRUD, search_builtin_functions
- `entity_tools.py` — connection, workflow, template, filler, uripath, event definition/action CRUD

## Build & Run

```bash
cd nocode-ai
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# Start the service
uvicorn app.main:app --reload --port 5001

# Ingest RAG documents
python scripts/ingest.py
```

## Configuration

Configuration is loaded in priority order:
1. Environment variables (highest)
2. Spring Cloud Config Server values
3. Default values in `app/config.py`

### Key Settings

| Setting | Default | Description |
|---------|---------|-------------|
| `LLM_PROVIDER` | `anthropic` | `anthropic` or `openai` |
| `ANTHROPIC_API_KEY` | — | Claude API key |
| `OPENAI_API_KEY` | — | OpenAI API key (if using openai provider) |
| `CLAUDE_SONNET` | `claude-sonnet-4-20250514` | Model for balanced tasks |
| `CLAUDE_HAIKU` | `claude-haiku-4-5-20251001` | Model for fast/cheap tasks |
| `GATEWAY_URL` | `http://localhost:8080` | nocode-saas Gateway URL |
| `COMPONENT_CATALOG_URL` | — | CDN URL for component-catalog.json |
| `AGENT_MODEL_TIER` | `balanced` | Model tier for agent (fast/balanced) |
| `MAX_AGENT_TURNS` | `50` | Max tool-use loop turns per request |
| `AGENT_MAX_TOKENS` | `16384` | Max output tokens per LLM call |
| `EUREKA_SERVER` | `http://localhost:9999/eureka/` | Eureka service discovery |
| `REDIS_URL` | `redis://localhost:6379` | Redis for rate limiting |
| `AI_TRACKING_ENABLED` | `true` | MySQL session tracking |
| `PROMPT_CACHING_ENABLED` | `true` | Anthropic prompt caching |

## Backend Integration (nocode-saas)

Agents call nocode-saas REST APIs through the **Gateway** (port 8080). The Gateway validates JWT tokens and routes to the correct microservice via Eureka.

### Microservices

| Service | Port | API Prefix | Manages |
|---------|------|------------|---------|
| Security | 8003 | `/api/security/` | Auth, users, clients, apps, permissions |
| Core | 8001 | `/api/core/` | Functions, schemas, connections, workflows, templates |
| UI | 8002 | `/api/ui/` | Pages, styles, themes, applications, URI paths |
| Multi | 8009 | `/api/multi/` | App creation/deletion, transport (export/import) |
| Files | 8004 | `/api/files/` | File storage, images |

### Auth Flow

Frontend JWT → Python agent (forwarded in `Authorization` header) → Gateway validates → backend service → `SecurityContextUtil` provides tenant-scoped access.

### Required Headers for Backend Calls

```
Authorization: Bearer {jwt_token}   (or just the token)
clientCode: {client_code}
appCode: {app_code}
Content-Type: application/json
```

## Services Reference

### LLM Provider (`app/services/llm_provider.py`)
- Abstraction over Anthropic Claude and OpenAI GPT
- Model tiers: `fast` (Haiku/gpt-4o-mini) and `balanced` (Sonnet/gpt-4o)
- Supports prompt caching (Anthropic only), vision/image inputs, tool-use
- Use `get_llm_provider()` singleton

### Eureka (`app/services/eureka.py`)
- Registers this service with Eureka on startup
- Deregisters on shutdown

### Config Server (`app/services/config_server.py`)
- Fetches config from Spring Cloud Config Server on startup
- URL: `http://config-server:8888/{service}/{profile}`

### Session Manager (`app/services/session_manager.py`)
- Tracks AI sessions in MySQL `ai_tracking_sessions` table
- Records: session_id, client/user info, token usage, request count

### Redis Client (`app/services/redis_client.py`)
- Rate limiting (per-user requests/minute and requests/hour)
- Request deduplication (prevents duplicate concurrent requests)

## RAG System

- **Vector Store**: ChromaDB (persistent, stored in `data/chroma/`)
- **Embedding Model**: `BAAI/bge-small-en-v1.5` via FastEmbed
- **Document Sources**: `../nocode-ui/ui-app/aicontext/` (platform docs) + `definitions/` (examples)
- **Ingestion**: `python scripts/ingest.py`

## SSE Streaming Protocol

All agent endpoints return SSE streams. Event types:

```
event: text          — Agent's markdown text (streamed incrementally)
event: tool_start    — Agent is calling a tool (name + input)
event: tool_result   — Tool execution result (success/failure + summary)
event: error         — Error occurred
event: done          — Agent turn complete (session_id + token usage)
```

## Component Catalog

The agent uses a dynamic component catalog generated from nocode-ui source files at build time:

1. **Build-time**: `scripts/generate-component-catalog.ts` (in nocode-ui) scans component property files using TypeScript compiler API → outputs `component-catalog.json`
2. **CI/CD**: Catalog JSON uploaded to CDN alongside UI dist files
3. **Startup**: `app/agents/appbuilder/catalog.py` fetches catalog from CDN
4. **Context**: Catalog is formatted and injected into agent's system prompt so it knows valid properties per component type

## Testing

```bash
pytest tests/
```

## Dependencies

Key packages: `fastapi`, `uvicorn`, `sse-starlette`, `anthropic`, `openai`, `httpx`, `chromadb`, `fastembed`, `aiomysql`, `redis`
