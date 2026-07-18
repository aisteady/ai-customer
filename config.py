"""
AI 客服 — 配置加载
==================

优先读本目录 `.env`，再尝试继承中台根目录 `.env`（DASHSCOPE_API_KEY 等）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

_DEMO_DIR = Path(__file__).resolve().parent


def _load_env() -> None:
    load_dotenv(_DEMO_DIR / ".env")
    # models/ai_customer → parents[2] = 仓库根
    if len(_DEMO_DIR.parents) >= 3:
        root_env = _DEMO_DIR.parents[2] / ".env"
        if root_env.exists():
            load_dotenv(root_env, override=False)


_load_env()


def env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


@dataclass(frozen=True)
class Settings:
    mcp_host: str = env("MCP_HOST", "127.0.0.1")
    mcp_port: int = int(env("MCP_PORT", "8765") or "8765")
    mcp_tcp_secret: str = env("MCP_TCP_SECRET")
    mcp_timeout: float = float(env("MCP_TIMEOUT", "120") or "120")
    project_id: str = env("PROJECT_ID")
    dashscope_api_key: str = env("DASHSCOPE_API_KEY")
    llm_model: str = env("LLM_MODEL", "qwen-plus")
    top_k: int = int(env("TOP_K", "5") or "5")
    search_threshold: float = float(env("SEARCH_THRESHOLD", "0.45") or "0.45")
    sqlite_path: Path = _DEMO_DIR / env("SQLITE_PATH", "data/chat.db")
    demo_dir: Path = _DEMO_DIR


settings = Settings()
