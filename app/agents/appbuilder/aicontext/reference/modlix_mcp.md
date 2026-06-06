---
name: project-modlix-mcp
description: Active project — MCP server at /Users/kirangrandhi/kiran/fincity/modlix-mcp exposing the entire Modlix platform to Claude Code. Repo at github.com/serialcoder13/modlix-mcp.
metadata: 
  node_type: memory
  type: project
  originSessionId: 0a3b792f-b0ea-4757-9c52-ac7f531b7154
---

modlix-mcp is the Claude-Code-facing MCP server for building Modlix applications and sites. Replaces the (renamed) nocode-ui-ai scaffold.

**Repo:** github.com/serialcoder13/modlix-mcp (private), default branch `main`.
**Local path:** /Users/kirangrandhi/kiran/fincity/modlix-mcp
**Package:** `modlix_mcp/` (Python), installed editable in `.venv/`.

**Why:** "nocode-ui-ai" was wrong because the platform is more than UI — apps, sites, functions, schemas, storages, themes, styles, URIPaths, fillers, mobileApps, personalizations, eventDefinitions/eventActions, plus the entire security service. Kiran wants typed MCP tools for the whole surface so Claude Code can build full applications end-to-end.

**How to apply:**
- All new platform-tool work goes in modlix-mcp, not nocode-ai's AppBuilder agent (which uses meta-tool dispatch unsuited to MCP).
- Tool modules live in `modlix_mcp/tools/`; each exports `register(mcp)`; `server.py` calls them all.
- Cross-cutting grammar/validators (authority strings, Kirun expression language, parameterMap, dependentStatements, breakpoints, primitive catalog, override predicates) live in `modlix_mcp/conventions.py` — every tool imports from here.
- Kirun function authoring uses kirun-py's DSL compiler (`modlix_mcp/kirun_dsl.py`) so agents can write functions as text instead of building step-graph JSON.
- Kiran prefers domain-by-domain depth-first builds, not shallow CRUD across many domains.
- Commits are buffered — push when a domain feels stable, not per-tool.

See also: [[feedback-storage-db-readonly]] for the data-layer separation rule.
