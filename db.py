"""
PostgreSQL 连接（会话数据进数据库，不写项目目录文件）
====================================================

复用中台同一套 DB_* 配置（本目录 .env 可覆盖），表落在独立 schema：
  ai_customer.sessions / ai_customer.messages
"""

from __future__ import annotations

from urllib.parse import quote_plus

import psycopg
from psycopg.rows import dict_row

from config import settings


def build_dsn() -> str:
    if settings.database_url:
        url = settings.database_url
        # SQLAlchemy 风格 → psycopg
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
    return psycopg.connect(build_dsn(), row_factory=dict_row)
