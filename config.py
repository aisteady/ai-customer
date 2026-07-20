"""
AI 客服 — 配置加载
==================

优先读本目录 `.env`，再尝试继承中台根目录 `.env`（不覆盖已有键）。

会话数据：PostgreSQL（DB_* / DATABASE_URL），schema 默认 ai_customer。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

_DEMO_DIR = Path(__file__).resolve().parent


def _repo_root_env() -> Path | None:
    """向上查找含 pyproject.toml 的仓库根，再取其 .env。"""
    for parent in _DEMO_DIR.parents:
        if (parent / "pyproject.toml").exists():
            env_path = parent / ".env"
            return env_path if env_path.exists() else None
    return None


def _load_env() -> None:
    load_dotenv(_DEMO_DIR / ".env")
    root_env = _repo_root_env()
    if root_env is not None:
        load_dotenv(root_env, override=False)


_load_env()


def env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


@dataclass(frozen=True)
class Settings:
    mcp_transport: str = env("MCP_TRANSPORT", "http")
    mcp_url: str = env("MCP_URL", "http://127.0.0.1:8765/mcp")
    mcp_client_token: str = env("MCP_CLIENT_TOKEN")
    mcp_host: str = env("MCP_HOST", "127.0.0.1")
    mcp_port: int = int(env("MCP_PORT", "8766") or "8766")
    mcp_tcp_secret: str = env("MCP_TCP_SECRET")
    mcp_timeout: float = float(env("MCP_TIMEOUT", "120") or "120")
    project_id: str = env("PROJECT_ID")
    dashscope_api_key: str = env("DASHSCOPE_API_KEY")
    llm_model: str = env("LLM_MODEL", "qwen-plus")
    top_k: int = int(env("TOP_K", "5") or "5")
    search_threshold: float = float(env("SEARCH_THRESHOLD", "0.45") or "0.45")

    # PostgreSQL（与中台共用；可被本目录 .env 覆盖）
    database_url: str = env("DATABASE_URL")
    db_host: str = env("DB_HOST", "localhost")
    db_port: int = int(env("DB_PORT", "5432") or "5432")
    db_user: str = env("DB_USER", "postgres")
    db_password: str = env("DB_PASSWORD", "password")
    db_name: str = env("DB_NAME", "aibase")
    db_schema: str = env("APP_DB_SCHEMA", "ai_customer")

    demo_dir: Path = _DEMO_DIR


settings = Settings()
