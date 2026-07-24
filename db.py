"""
PostgreSQL 连接（db.py）
=======================

学习要点
--------
会话 / harness_events 进数据库，不写项目目录 SQLite。
DSN 来自 config.settings（DATABASE_URL 或 DB_*）。
LangGraph PostgresSaver 也复用 build_dsn()（见 graph/build.py）。

表落在独立 schema（默认 ai_customer），与中台知识库表隔离。
"""

from __future__ import annotations

from urllib.parse import quote_plus

import psycopg
from psycopg.rows import dict_row

from config import settings


def build_dsn() -> str:
    """拼 psycopg 可用的 postgresql:// URL。"""
    if settings.database_url:
        url = settings.database_url
        # 兼容 SQLAlchemy 风格前缀
        return url.replace("postgresql+asyncpg://", "postgresql://").replace(
            "postgresql+psycopg://", "postgresql://"
        )
    user = quote_plus(settings.db_user)
    password = quote_plus(settings.db_password)
    return (
        f"postgresql://{user}:{password}"
        f"@{settings.db_host}:{settings.db_port}/{settings.db_name}"
    )


def connect():
    """返回 dict_row 连接：fetchone()['id'] 而不是下标。"""
    return psycopg.connect(build_dsn(), row_factory=dict_row)
