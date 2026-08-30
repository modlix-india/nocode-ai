"""File-based session store for standalone mode (no MySQL).

Stores sessions and history as JSON files in a configurable directory.
Used as an automatic fallback when the MySQL pool is unavailable.

File layout:
    {base_dir}/
        sessions/
            {session_id}.json          # Session metadata + context
        history/
            {session_id}/
                turn_{turn_number}.json  # Per-turn conversation data
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_DEFAULT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "standalonedb")


class FileStore:
    """Simple JSON file store for session persistence without a database."""

    def __init__(self, base_dir: str | None = None) -> None:
        self._base = Path(base_dir or _DEFAULT_DIR)
        self._sessions_dir = self._base / "sessions"
        self._history_dir = self._base / "history"
        self._sessions_dir.mkdir(parents=True, exist_ok=True)
        self._history_dir.mkdir(parents=True, exist_ok=True)
        logger.info("FileStore initialized at %s", self._base)

    # ── Session CRUD ─────────────────────────────────────────────

    def save_session(self, session_id: str, data: dict[str, Any]) -> None:
        """Write or overwrite session metadata."""
        data["updated_at"] = time.time()
        if "created_at" not in data:
            data["created_at"] = data["updated_at"]
        path = self._sessions_dir / f"{session_id}.json"
        path.write_text(json.dumps(data, default=str), encoding="utf-8")

    def load_session(self, session_id: str) -> dict[str, Any] | None:
        """Load session metadata by ID. Returns None if not found."""
        path = self._sessions_dir / f"{session_id}.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to read session file %s: %s", path, e)
            return None

    def delete_session(self, session_id: str) -> bool:
        """Delete session and its history."""
        path = self._sessions_dir / f"{session_id}.json"
        deleted = False
        if path.exists():
            path.unlink()
            deleted = True
        # Remove history directory
        hist_dir = self._history_dir / session_id
        if hist_dir.exists():
            for f in hist_dir.iterdir():
                f.unlink()
            hist_dir.rmdir()
        return deleted

    def list_sessions(
        self,
        user_id: int,
        client_code: str,
        agent_name: str | None = None,
        limit: int = 20,
        offset: int = 0,
        app_code: str | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        """List sessions for a user, newest first."""
        all_sessions: list[dict[str, Any]] = []
        for path in self._sessions_dir.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if data.get("user_id") != user_id:
                continue
            if data.get("client_code") != client_code:
                continue
            if agent_name and data.get("agent_name") != agent_name:
                continue
            if app_code and data.get("app_code") != app_code:
                continue
            all_sessions.append(data)

        all_sessions.sort(key=lambda s: s.get("updated_at", 0), reverse=True)
        total = len(all_sessions)
        return all_sessions[offset:offset + limit], total

    # ── History (conversation turns) ─────────────────────────────

    def save_turn(
        self,
        session_id: str,
        turn_number: int,
        user_instruction: str,
        assistant_summary: str,
        tool_calls_json: str | None = None,
        model: str | None = None,
    ) -> None:
        """Save or overwrite a conversation turn."""
        hist_dir = self._history_dir / session_id
        hist_dir.mkdir(parents=True, exist_ok=True)
        data = {
            "turn_number": turn_number,
            "user_instruction": user_instruction,
            "assistant_summary": assistant_summary,
            "tool_calls_json": tool_calls_json,
            "model": model,
            "timestamp": time.time(),
        }
        path = hist_dir / f"turn_{turn_number}.json"
        path.write_text(json.dumps(data, default=str), encoding="utf-8")

    def load_history(
        self, session_id: str, limit: int = 100, offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        """Load conversation turns for a session, sorted by turn number."""
        hist_dir = self._history_dir / session_id
        if not hist_dir.exists():
            return [], 0

        turns: list[dict[str, Any]] = []
        for path in hist_dir.glob("turn_*.json"):
            try:
                turns.append(json.loads(path.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, OSError):
                continue

        turns.sort(key=lambda t: t.get("turn_number", 0))
        total = len(turns)
        return turns[offset:offset + limit], total


# ── Singleton ────────────────────────────────────────────────────

_file_store: FileStore | None = None


def get_file_store() -> FileStore:
    """Get the file store singleton (lazy init)."""
    global _file_store
    if _file_store is None:
        _file_store = FileStore()
    return _file_store
