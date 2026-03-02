# Contributing to Nocode AI

Thank you for your interest in contributing to the Nocode AI service. This guide will help you get started.

## Table of Contents

- [Prerequisites](#prerequisites)
- [Development Setup](#development-setup)
- [Project Structure](#project-structure)
- [Architecture Overview](#architecture-overview)
- [Adding Tools](#adding-tools)
- [Adding a New Agent](#adding-a-new-agent)
- [Code Conventions](#code-conventions)
- [API Development](#api-development)
- [Database Migrations](#database-migrations)
- [Configuration](#configuration)
- [Docker](#docker)
- [Submitting Changes](#submitting-changes)

---

## Prerequisites

- **Python 3.9+** (3.11 recommended for production parity)
- **pip** for dependency management
- An **Anthropic API key** (or OpenAI key for alternate provider)
- Optionally: running **nocode-saas** services (Config Server on `:8888`, Eureka on `:9999`, Gateway on `:8080`)
- **MySQL 8.0+** for session tracking (optional for local dev)
- **Redis** for rate limiting (optional for local dev)

## Development Setup

### 1. Clone and Create Virtual Environment

```bash
cd nocode-ai
python3 -m venv venv
source venv/bin/activate
```

### 2. Install Dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure Environment

Create a `.env` file (already in `.gitignore`):

```bash
ANTHROPIC_API_KEY=sk-ant-your-key-here

# For standalone development (no config server / eureka needed)
CONFIG_SERVER_ENABLED=false
EUREKA_ENABLED=false
```

### 4. Ingest RAG Documents (First Time)

```bash
python scripts/ingest.py
```

### 5. Start the Service

```bash
# Development with hot reload
uvicorn app.main:app --reload --port 5001

# Or use the startup script
./scripts/start-local.sh
```

### 6. Verify

```bash
curl http://localhost:5001/health
# → {"status": "UP", ...}

curl http://localhost:5001/health/detailed
# → detailed component health
```

---

## Project Structure

```
nocode-ai/
├── app/
│   ├── main.py                         # FastAPI entry point + lifespan
│   ├── config.py                       # Pydantic Settings (all config)
│   ├── core/                           # Shared agentic framework
│   │   ├── agent.py                    # BaseAgent — tool-use loop
│   │   ├── context.py                  # System prompt builder
│   │   ├── session.py                  # Session model
│   │   ├── streaming.py               # SSE protocol
│   │   └── tools/
│   │       ├── base.py                 # ToolDefinition, ToolResult
│   │       └── http_client.py          # SaasClient (httpx wrapper)
│   ├── agents/                         # Domain-specific agents
│   │   └── appbuilder/                 # AppBuilder agent
│   │       ├── agent.py                # AppBuilderAgent (extends BaseAgent)
│   │       ├── context.py              # Dynamic context builder
│   │       ├── catalog.py              # Component catalog
│   │       ├── router.py               # Chat + session endpoints
│   │       ├── AGENT.md                # Agent specification
│   │       └── tools/                  # 60+ tool implementations
│   │           ├── registry.py         # ALL_TOOLS export list
│   │           ├── _shared.py          # SaasClient singleton
│   │           ├── _executor.py        # Page read-modify-write executor
│   │           ├── page_tools.py       # Page CRUD
│   │           ├── component_tools.py  # Component CRUD
│   │           ├── batch_tools.py      # Batch operations
│   │           ├── event_tools.py      # Event function CRUD
│   │           ├── application_tools.py# App CRUD
│   │           ├── style_tools.py      # Theme & style CRUD
│   │           ├── function_tools.py   # Function & schema CRUD
│   │           ├── entity_tools.py     # Connection, workflow, template
│   │           └── version_tools.py    # Version control tools
│   ├── services/                       # Shared services
│   │   ├── llm_provider.py            # Per-agent LLM provider (Anthropic/OpenAI)
│   │   ├── eureka.py                  # Service discovery
│   │   ├── config_server.py           # Spring Cloud Config client
│   │   ├── security.py                # Token validation
│   │   ├── session_manager.py         # Session persistence (MySQL)
│   │   ├── context_manager.py         # Conversation history
│   │   ├── token_tracker.py           # Token usage tracking
│   │   └── redis_client.py            # Rate limiting & caching
│   ├── api/                            # HTTP API layer
│   │   ├── routes/
│   │   │   ├── health.py              # Health checks
│   │   │   └── query.py               # RAG query endpoint
│   │   └── models/
│   │       ├── auth.py                # Auth models
│   │       └── requests.py            # Request/response DTOs
│   ├── rag/                            # RAG system
│   │   ├── engine.py                  # ChromaDB + FastEmbed
│   │   ├── embeddings.py              # Embedding model selection
│   │   └── retriever.py               # Retrieval logic
│   ├── db/                             # Database layer
│   │   ├── models.py                  # Pydantic models
│   │   ├── connection.py              # aiomysql pool
│   │   └── migrations.py             # Flyway migration runner
│   ├── middleware/
│   │   └── rate_limiter.py            # Rate limiting + deduplication
│   └── utils/
├── migrations/                         # SQL migration scripts (Flyway-style)
├── scripts/                            # Startup & utility scripts
├── data/chroma/                        # ChromaDB persistence (gitignored)
├── definitions/                        # Example definitions for RAG
├── docs/                               # Documentation
├── Dockerfile
└── requirements.txt
```

---

## Architecture Overview

### Agentic Tool-Use Loop

The service follows a **single-agent, multi-tool** pattern (similar to Claude Code):

```
User Message
    ↓
Build System Prompt (static docs + dynamic context)
    ↓
Call LLM with tools → Stream text via SSE
    ↓
If tool_use → Execute tool → Emit result → Loop back to LLM
    ↓
If end_turn → Done → Emit final stats
```

### Key Design Principle

Large objects (pages can be 30K+ JSON) are **never exposed to the agent**. Instead:
- `read_page_structure` returns a compact tree (keys, types, parents)
- The agent operates at the **component level** (`add_component`, `update_component`, etc.)
- A Python executor handles full **read-modify-write** internally

### Core Classes

| Class | Location | Purpose |
|-------|----------|---------|
| `BaseAgent` | `app/core/agent.py` | Core tool-use loop, streaming, tool execution |
| `AppBuilderAgent` | `app/agents/appbuilder/agent.py` | Extends BaseAgent with app-building context |
| `ToolDefinition` | `app/core/tools/base.py` | Tool schema: name, display_name, description, params, execute fn |
| `ToolResult` | `app/core/tools/base.py` | Tool output: success, data, summary, error |
| `SaasClient` | `app/core/tools/http_client.py` | Shared httpx client for backend API calls |
| `Settings` | `app/config.py` | All configuration via pydantic-settings |

---

## Adding Tools

Tools are the primary extension point. Each tool is a `ToolDefinition` with a JSON schema and an async execute function.

### 1. Define the Tool

Create or edit a file in `app/agents/appbuilder/tools/`:

```python
# app/agents/appbuilder/tools/my_tools.py
from app.core.tools.base import ToolDefinition, ToolParameter, ToolResult

async def execute_my_tool(params: dict, context: dict) -> ToolResult:
    """Tool execution function."""
    name = params["name"]
    client = context["client"]          # SaasClient instance
    headers = context["headers"]        # Auth headers (clientCode, appCode, Authorization)
    app_code = context["app_code"]

    try:
        response = await client.get(f"/api/some/endpoint/{name}", headers=headers)
        return ToolResult(success=True, data=response, summary=f"Fetched {name}")
    except Exception as e:
        return ToolResult(success=False, error=str(e))

my_tool = ToolDefinition(
    name="my_tool",
    display_name="My Tool",
    description="Does something useful. Use this when you need to ...",
    parameters=[
        ToolParameter(name="name", type="string", description="The name", required=True),
        ToolParameter(name="optional_flag", type="boolean", description="Enable X", required=False, default=False),
    ],
    execute=execute_my_tool,
)
```

### 2. Register the Tool

Add it to the registry in `app/agents/appbuilder/tools/registry.py`:

```python
from .my_tools import my_tool

ALL_TOOLS: list[ToolDefinition] = [
    # ... existing tools ...
    my_tool,
]
```

### 3. Tool Guidelines

- **Tool names**: Use `snake_case` (e.g., `add_component`, `read_page_structure`)
- **Display names**: Provide a human-friendly `display_name` (e.g., `"Add Component"`) — shown in the UI
- **Descriptions**: Write for the LLM — explain *when* and *why* to use the tool
- **Parameters**: Be explicit about types, enums, and defaults
- **Return ToolResult**: Always return `ToolResult`, never raise exceptions
- **Use the shared SaasClient**: Access it via `context["client"]` — do not create new HTTP clients
- **Keep tools focused**: One tool = one action. Prefer multiple small tools over one large one

---

## Adding a New Agent

To add a new agent (e.g., a "DataBuilder" agent):

### 1. Create the Agent Directory

```
app/agents/databuilder/
├── agent.py          # DataBuilderAgent(BaseAgent)
├── context.py        # System prompt builder
├── router.py         # FastAPI router
├── AGENT.md          # Agent specification doc
└── tools/
    ├── registry.py   # ALL_TOOLS list
    └── data_tools.py # Tool implementations
```

### 2. Extend BaseAgent

Each agent can specify its own LLM provider via the `provider` parameter (defaults to the global `LLM_PROVIDER` setting):

```python
# app/agents/databuilder/agent.py
from app.core.agent import BaseAgent
from app.config import settings

class DataBuilderAgent(BaseAgent):
    def __init__(self, provider: str = "anthropic"):
        super().__init__(
            name="databuilder",
            tools=ALL_TOOLS,
            context_builder=load_context(),
            provider=provider,  # "anthropic" or "openai" — per-agent choice
        )

    def build_dynamic_context(self, session) -> str:
        """Return per-request context appended to the system prompt."""
        return f"Current database: {session.context.get('db_name', 'default')}"
```

The `provider` parameter controls which LLM backend is used for that agent's tool-use loop. Multiple agents can use different providers simultaneously (e.g., AppBuilder uses Anthropic while AdBuilder uses OpenAI).

### 3. Create a Router

```python
# app/agents/databuilder/router.py
from fastapi import APIRouter
router = APIRouter(prefix="/api/ai/databuilder", tags=["databuilder"])

@router.post("/chat")
async def chat(...):
    ...
```

### 4. Register in `main.py`

```python
from app.agents.databuilder.router import router as databuilder_router
app.include_router(databuilder_router)
```

---

## Code Conventions

### Python Style

- **PEP 8** compliance (no linter config enforced yet — follow existing patterns)
- **Type hints** on all function signatures
- **Async/await** everywhere — no blocking I/O
- **Snake_case** for functions and variables, **PascalCase** for classes
- **Pydantic models** for API request/response DTOs
- **Dataclasses** for internal data structures

### Logging

```python
import logging
logger = logging.getLogger(__name__)

logger.info("Processing request for session %s", session_id)
logger.error("Failed to execute tool %s: %s", tool_name, error)
```

### Error Handling

- Tools return `ToolResult(success=False, error="...")` — never raise
- API endpoints raise `HTTPException` for client errors
- Use `try/except` around external service calls (backend API, LLM, Redis)

### Async Patterns

```python
# Good — async all the way
async def fetch_data(client: SaasClient, headers: dict) -> dict:
    return await client.get("/api/endpoint", headers=headers)

# Bad — blocking call in async context
def fetch_data_sync(url: str) -> dict:
    return requests.get(url).json()  # NEVER do this
```

---

## API Development

### Adding an Endpoint

1. Create or edit a route file in `app/api/routes/` or an agent's `router.py`
2. Define request/response models in `app/api/models/`
3. Include the router in `app/main.py`

### SSE Streaming

The service uses Server-Sent Events for real-time streaming:

```python
from sse_starlette.sse import EventSourceResponse

async def event_generator():
    yield {"event": "text", "data": json.dumps({"content": "Hello"})}
    yield {"event": "done", "data": json.dumps({"session_id": "abc"})}

return EventSourceResponse(event_generator())
```

### SSE Event Types

| Event | Purpose |
|-------|---------|
| `text` | Agent text chunk |
| `tool_start` | Tool execution starting |
| `tool_result` | Tool result (success/failure) |
| `error` | Error occurred |
| `done` | Agent finished (includes session_id, token usage) |
| `keepalive` | Connection ping |

### Auth Headers

All authenticated endpoints require:

```
Authorization: Bearer {jwt_token}
clientCode: {client_code}
appCode: {app_code}
```

---

## Database Migrations

Migrations use a Flyway-style naming convention in `migrations/`:

```
migrations/
├── V1__Initial_AI_Tracking.sql
├── V2__Add_Object_And_Agent_Name.sql
├── V3__Add_Session_Title.sql
├── V4__Add_Session_Context.sql
├── V5__Add_Turn_Tool_Calls.sql
├── V6__Add_Processing_Status.sql
└── V7__Unique_Session_Turn.sql
```

### Adding a Migration

1. Create a new file: `V{N}__{Description}.sql` (double underscore, increment N)
2. Write forward-only SQL (no rollbacks)
3. Migrations run automatically on startup via `app/db/migrations.py`

### Key Tables

| Table | Purpose |
|-------|---------|
| `ai_tracking_sessions` | Session metadata, token totals, status (ACTIVE/PROCESSING/COMPLETED/EXPIRED) |
| `ai_token_usage` | Per-request token tracking |
| `ai_session_history` | Turn-by-turn conversation log (with tool_calls_json, incremental upsert) |

---

## Configuration

Configuration is managed via `app/config.py` using `pydantic-settings`.

### Priority Order

1. **Environment variables** (highest)
2. **Spring Cloud Config Server** values (if `CONFIG_SERVER_ENABLED=true`)
3. **Defaults** in `Settings` class

### Key Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `LLM_PROVIDER` | `anthropic` | Global default LLM provider (`anthropic` or `openai`) |
| `APPBUILDER_PROVIDER` | `anthropic` | Per-agent override for AppBuilder |
| `AGENT_MODEL_TIER` | `balanced` | Model tier (`fast` = Haiku, `balanced` = Sonnet) |
| `MAX_AGENT_TURNS` | `50` | Max tool-use iterations per request |
| `AGENT_MAX_TOKENS` | `16384` | Max tokens per LLM response |
| `PROMPT_CACHING_ENABLED` | `true` | Anthropic prompt caching (~90% savings) |
| `RATE_LIMIT_PER_MINUTE` | `10` | Per-user rate limit |
| `RATE_LIMIT_PER_HOUR` | `100` | Per-user hourly limit |
| `CONFIG_SERVER_ENABLED` | `true` | Fetch config from Spring Cloud |
| `EUREKA_ENABLED` | `true` | Register with Eureka |

---

## Docker

### Build

```bash
docker build -t nocode-ai .
```

### Run

```bash
docker run -p 5001:5001 \
  -e ANTHROPIC_API_KEY=sk-ant-... \
  -e CONFIG_SERVER_ENABLED=false \
  -e EUREKA_ENABLED=false \
  nocode-ai
```

### Production

The Dockerfile uses Gunicorn with UvicornWorker (4 workers, 300s timeout). The production startup script is at `scripts/start-production.sh`.

---

## Submitting Changes

### Branch Naming

Use descriptive branch names:

```
feature/add-data-tools
fix/session-timeout-bug
refactor/tool-registry
```

### Pull Request Guidelines

1. **Keep PRs focused** — one feature or fix per PR
2. **Describe the change** — what, why, and how to test
3. **Update documentation** — if you add tools, agents, or endpoints, update relevant docs
4. **No secrets** — never commit API keys or `.env` files
5. **Test locally** — verify the service starts and your change works end-to-end

### Checklist Before Submitting

- [ ] Service starts without errors (`uvicorn app.main:app --reload --port 5001`)
- [ ] New tools return `ToolResult` (not exceptions)
- [ ] New endpoints have proper auth header checks
- [ ] No hardcoded URLs or API keys
- [ ] Added to tool registry if applicable
- [ ] Migration file follows `V{N}__` naming

---

## License

Proprietary - Modlix
