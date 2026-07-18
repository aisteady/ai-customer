"""
AI 客服 — 本地会话存储（SQLite）
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class Message:
    id: str
    session_id: str
    role: str
    content: str
    sources: str | None
    trace_id: str | None
    created_at: str


class ChatStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    title TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    sources TEXT,
                    trace_id TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES sessions(id)
                );
                CREATE INDEX IF NOT EXISTS ix_messages_session
                ON messages(session_id, created_at);
                """
            )

    def create_session(self, title: str = "新会话") -> str:
        sid = str(uuid.uuid4())
        now = _utc_now()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO sessions (id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (sid, title, now, now),
            )
        return sid

    def list_sessions(self, limit: int = 50) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, title, created_at, updated_at FROM sessions "
                "ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def add_message(
        self,
        session_id: str,
        role: str,
        content: str,
        *,
        sources: str | None = None,
        trace_id: str | None = None,
    ) -> str:
        mid = str(uuid.uuid4())
        now = _utc_now()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO messages (id, session_id, role, content, sources, trace_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (mid, session_id, role, content, sources, trace_id, now),
            )
            conn.execute(
                "UPDATE sessions SET updated_at = ?, title = CASE "
                "WHEN title = '新会话' AND ? = 'user' THEN substr(?, 1, 40) ELSE title END "
                "WHERE id = ?",
                (now, role, content, session_id),
            )
        return mid

    def list_messages(self, session_id: str) -> list[Message]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, session_id, role, content, sources, trace_id, created_at "
                "FROM messages WHERE session_id = ? ORDER BY created_at ASC",
                (session_id,),
            ).fetchall()
        return [
            Message(
                id=r["id"],
                session_id=r["session_id"],
                role=r["role"],
                content=r["content"],
                sources=r["sources"],
                trace_id=r["trace_id"],
                created_at=r["created_at"],
            )
            for r in rows
        ]
