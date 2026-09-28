"""Common building blocks for every adzump agent (orchestrator and sub-agents).

- ``journey``         - the step engine: an agent's ordered steps, walked each
                        turn into step states and the missing list
- ``dynamic_context`` - ``DynamicContext``: an agent's journey plus its own
                        fixed text, rendered into the per-turn reminder

Nothing here knows about one agent's domain; each agent declares its own
journey and ``DynamicContext`` (the orchestrator's live in ``workflow.py``).
"""
