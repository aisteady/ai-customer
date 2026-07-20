"""
AI 客服 — 会话存储（PostgreSQL）
================================

数据在中台同一 PostgreSQL 的 schema `ai_customer` 中，
不再写入项目目录下的 SQLite 文件。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from db import connect
from config import settings


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
    def __init__(self, schema: str | None = None) -> None:
        self.schema = schema or settings.db_schema
        self._init_schema()

    def _q(self, table: str) -> str:
        return f'"{self.schema}".{table}'

    def _init_schema(self) -> None:
        with connect() as conn:
            conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{self.schema}"')
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {self._q("sessions")} (
                    id TEXT PRIMARY KEY,
                    title TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {self._q("messages")} (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL REFERENCES {self._q("sessions")}(id),
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    sources TEXT,
                    trace_id TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                f"""
                CREATE INDEX IF NOT EXISTS ix_ai_customer_messages_session
                ON {self._q("messages")}(session_id, created_at)
                """
            )
            conn.commit()

    def create_session(self, title: str = "新会话") -> str:
        sid = str(uuid.uuid4())
        now = _utc_now()
        with connect() as conn:
            conn.execute(
                f"INSERT INTO {self._q('sessions')} (id, title, created_at, updated_at) "
                f"VALUES (%s, %s, %s, %s)",
                (sid, title, now, now),
            )
            conn.commit()
        return sid

    def list_sessions(self, limit: int = 50) -> list[dict]:
        with connect() as conn:
            rows = conn.execute(
                f"SELECT id, title, created_at, updated_at FROM {self._q('sessions')} "
                f"ORDER BY updated_at DESC LIMIT %s",
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
        with connect() as conn:
            conn.execute(
                f"INSERT INTO {self._q('messages')} "
                f"(id, session_id, role, content, sources, trace_id, created_at) "
                f"VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (mid, session_id, role, content, sources, trace_id, now),
            )
            conn.execute(
                f"UPDATE {self._q('sessions')} SET updated_at = %s, title = CASE "
                f"WHEN title = '新会话' AND %s = 'user' THEN left(%s, 40) ELSE title END "
                f"WHERE id = %s",
                (now, role, content, session_id),
            )
            conn.commit()
        return mid

    def list_messages(self, session_id: str) -> list[Message]:
        with connect() as conn:
            rows = conn.execute(
                f"SELECT id, session_id, role, content, sources, trace_id, created_at "
                f"FROM {self._q('messages')} WHERE session_id = %s ORDER BY created_at ASC",
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
