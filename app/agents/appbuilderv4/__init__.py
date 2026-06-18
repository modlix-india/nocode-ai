"""AppBuilder v4 — code-first authoring agent.

The agent has ONE write primitive (`code_run`) which executes a Python
script in a subprocess sandbox. The sandbox imports a `modlix` SDK with
auth-bound HTTP helpers and a component-catalog reader, so the agent can
compose page definitions in code and post them to the gateway in one shot
instead of doing dozens of round-trip tool calls.

Coexists with the v3 agent at /api/ai/appbuilder/chat; v4 lives at
/api/ai/appbuilderv4/chat. v4 retires the old agent once it proves out.

See CLAUDE.md in this directory for the tool-add roadmap.
"""
