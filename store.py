"""
会话与审计存储（store.py）
==========================

学习要点
--------
业务数据落在中台同一 PostgreSQL 的独立 schema（默认 ai_customer），
与中台知识库表隔离，也与 LangGraph checkpoint 表（可同 schema）分工：

  sessions        — 会话元数据（title / role / thread_id）
  messages        — 对话气泡（含 sources、trace_id）
  harness_events  — Harness 审计（intent / plan / tools / finalize …）
  crm_leads       — 报价线索 JSON（模拟 CRM POST 登记）

thread_id：必须与 graph.invoke 的 configurable.thread_id 一致，
否则 resume_clarify 找不到 checkpoint。

启动时 _init_schema 会 CREATE IF NOT EXISTS，并尝试 ADD COLUMN
兼容旧库（只有 id/title 的 sessions）。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from config import settings
from db import connect


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class Message:
    """一行对话消息（list_messages 返回）。"""

    id: str
    session_id: str
    role: str
    content: str
    sources: str | None  # JSON 字符串，UI 再 loads
    trace_id: str | None
    created_at: str


class ChatStore:
    def __init__(self, schema: str | None = None) -> None:
        self.schema = schema or settings.db_schema
        self._init_schema()

    def _q(self, table: str) -> str:
        """带 schema 的限定表名，避免搜到 public 同名表。"""
        return f'"{self.schema}".{table}'

    def _init_schema(self) -> None:
        with connect() as conn:
            conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{self.schema}"')
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {self._q("sessions")} (
                    id TEXT PRIMARY KEY,
                    title TEXT,
                    role TEXT,
                    thread_id TEXT,
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
                CREATE TABLE IF NOT EXISTS {self._q("harness_events")} (
                    id TEXT PRIMARY KEY,
                    session_id TEXT,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL DEFAULT '{{}}',
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {self._q("crm_leads")} (
                    id TEXT PRIMARY KEY,
                    session_id TEXT,
                    payload_json TEXT NOT NULL,
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
            conn.execute(
                f"""
                CREATE INDEX IF NOT EXISTS ix_ai_customer_crm_leads_session
                ON {self._q("crm_leads")}(session_id, created_at)
                """
            )
            # 旧表可能缺列：存在则忽略
            for ddl in (
                f'ALTER TABLE {self._q("sessions")} ADD COLUMN IF NOT EXISTS role TEXT',
                f'ALTER TABLE {self._q("sessions")} '
                f'ADD COLUMN IF NOT EXISTS thread_id TEXT',
            ):
                try:
                    conn.execute(ddl)
                except Exception:
                    pass
            conn.commit()

    def create_session(
        self,
        title: str = "新会话",
        *,
        role: str | None = None,
        thread_id: str | None = None,
    ) -> str:
        sid = str(uuid.uuid4())
        tid = thread_id or sid
        now = _utc_now()
        with connect() as conn:
            conn.execute(
                f"INSERT INTO {self._q('sessions')} "
                f"(id, title, role, thread_id, created_at, updated_at) "
                f"VALUES (%s, %s, %s, %s, %s, %s)",
                (sid, title, role, tid, now, now),
            )
            conn.commit()
        return sid

    def update_session_meta(
        self,
        session_id: str,
        *,
        role: str | None = None,
        thread_id: str | None = None,
        title: str | None = None,
    ) -> None:
        sets: list[str] = ["updated_at = %s"]
        args: list[object] = [_utc_now()]
        if role is not None:
            sets.append("role = %s")
            args.append(role)
        if thread_id is not None:
            sets.append("thread_id = %s")
            args.append(thread_id)
        if title is not None:
            sets.append("title = %s")
            args.append(title)
        args.append(session_id)
        with connect() as conn:
            conn.execute(
                f"UPDATE {self._q('sessions')} SET {', '.join(sets)} WHERE id = %s",
                tuple(args),
            )
            conn.commit()

    def get_session(self, session_id: str) -> dict | None:
        with connect() as conn:
            row = conn.execute(
                f"SELECT id, title, role, thread_id, created_at, updated_at "
                f"FROM {self._q('sessions')} WHERE id = %s",
                (session_id,),
            ).fetchone()
        return dict(row) if row else None

    def list_sessions(self, limit: int = 50) -> list[dict]:
        with connect() as conn:
            rows = conn.execute(
                f"SELECT id, title, role, thread_id, created_at, updated_at "
                f"FROM {self._q('sessions')} "
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
        """
        追加一条消息；首条 user 消息会顺便把 title 改成问题前 40 字。
        """
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
                f"FROM {self._q('messages')} WHERE session_id = %s "
                f"ORDER BY created_at ASC",
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

    def log_harness_event(
        self,
        event_type: str,
        *,
        session_id: str | None = None,
        payload: dict | None = None,
    ) -> None:
        """供 CustomerHarness.emit 调用；查问题时按 session_id + created_at 排。"""
        with connect() as conn:
            conn.execute(
                f"""
                INSERT INTO {self._q("harness_events")}
                (id, session_id, event_type, payload_json, created_at)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    str(uuid.uuid4()),
                    session_id,
                    event_type,
                    json.dumps(payload or {}, ensure_ascii=False),
                    _utc_now(),
                ),
            )
            conn.commit()

    def save_crm_lead(
        self,
        payload: dict,
        *,
        session_id: str | None = None,
    ) -> str:
        """保存报价线索整包 JSON（模拟 CRM 登记）。返回 lead id。"""
        lead_id = str(uuid.uuid4())
        with connect() as conn:
            conn.execute(
                f"""
                INSERT INTO {self._q("crm_leads")}
                (id, session_id, payload_json, created_at)
                VALUES (%s, %s, %s, %s)
                """,
                (
                    lead_id,
                    session_id or payload.get("session_id") or None,
                    json.dumps(payload, ensure_ascii=False),
                    _utc_now(),
                ),
            )
            conn.commit()
        return lead_id
