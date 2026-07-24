"""
AI 客服 — 配置加载（config.py）
================================

学习要点
--------
1. 密钥/地址全部走环境变量，不写死在代码里。
2. 加载顺序：本目录 `.env` 先；仓库根 `.env` 后且 override=False
   → 本应用配置优先，缺的 DB 等再从中台根继承。
3. Settings 用 frozen dataclass：启动读一次，运行期不可改。

与流程强相关的旋钮：
  MAX_TOOL_LOOPS / MAX_CLARIFY_LOOPS — Harness 与图内 Loop 硬顶
  CHECKPOINT_SCHEMA / ALLOW_MEMORY_CHECKPOINT — interrupt 能否跨请求恢复
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

_DEMO_DIR = Path(__file__).resolve().parent
logger = logging.getLogger(__name__)


def _repo_root_env() -> Path | None:
    """向上找含 pyproject.toml 的仓库根，再取其 .env。"""
    for parent in _DEMO_DIR.parents:
        if (parent / "pyproject.toml").exists():
            env_path = parent / ".env"
            return env_path if env_path.exists() else None
    return None


def _load_env() -> None:
    """先本地后根目录；根目录不覆盖本地已有键。"""
    load_dotenv(_DEMO_DIR / ".env")
    root_env = _repo_root_env()
    if root_env is not None:
        load_dotenv(root_env, override=False)


# import 时立刻加载，保证 Settings 默认值能读到环境变量
_load_env()


def env(name: str, default: str = "") -> str:
    """读字符串并 strip，避免尾部空格导致 MCP 鉴权失败。"""
    return (os.getenv(name) or default).strip()


def env_bool(name: str, default: bool = False) -> bool:
    raw = env(name, "true" if default else "false").lower()
    return raw in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    """全局配置快照。字段分组见下方注释。"""

    environment: str = env("ENVIRONMENT", "development")

    # ---- MCP（默认官方 Streamable HTTP）----
    # MCP_CLIENT_TOKEN 必须等于中台网关密钥；勿用项目 SERVICE_TOKEN（那是 REST JWT）
    mcp_transport: str = env("MCP_TRANSPORT", "http")
    mcp_url: str = env("MCP_URL", "http://127.0.0.1:8765/mcp")
    mcp_client_token: str = env("MCP_CLIENT_TOKEN")
    mcp_host: str = env("MCP_HOST", "127.0.0.1")
    mcp_port: int = int(env("MCP_PORT", "8766") or "8766")
    mcp_tcp_secret: str = env("MCP_TCP_SECRET")
    mcp_timeout: float = float(env("MCP_TIMEOUT", "120") or "120")

    # ---- 中台项目 & LLM ----
    project_id: str = env("PROJECT_ID")
    # 素材检索项目（默认多模态素材中心）；空则回退 PROJECT_ID
    media_project_id: str = env(
        "MEDIA_PROJECT_ID", "667b0124-6a5b-413e-b80d-8199e0abb066"
    )
    # 空 = 不传 model，用中台「大模型管理」项目配置
    llm_model: str = env("LLM_MODEL")
    top_k: int = int(env("TOP_K", "5") or "5")
    search_threshold: float = float(env("SEARCH_THRESHOLD", "0.45") or "0.45")
    # 素材检索阈值（可低于文档；关键词命中时常向量分不高）
    media_search_threshold: float = float(
        env("MEDIA_SEARCH_THRESHOLD", "0.25") or "0.25"
    )

    # ---- Agent / Loop / Harness ----
    max_tool_loops: int = int(env("MAX_TOOL_LOOPS", "8") or "8")
    max_clarify_loops: int = int(env("MAX_CLARIFY_LOOPS", "10") or "10")

    # ---- 工艺配置助手（ai_quotation HTTP）----
    # 例：http://127.0.0.1:8510 或完整 http://127.0.0.1:8510/api/v1/recommend
    process_config_url: str = env("PROCESS_CONFIG_URL")
    process_config_token: str = env("PROCESS_CONFIG_TOKEN")
    process_config_timeout: float = float(
        env("PROCESS_CONFIG_TIMEOUT", "120") or "120"
    )

    # ---- PostgreSQL：业务表 +（可选）LangGraph checkpoint ----
    database_url: str = env("DATABASE_URL")
    db_host: str = env("DB_HOST", "localhost")
    db_port: int = int(env("DB_PORT", "5432") or "5432")
    db_user: str = env("DB_USER", "postgres")
    db_password: str = env("DB_PASSWORD", "password")
    db_name: str = env("DB_NAME", "aibase")
    db_schema: str = env("APP_DB_SCHEMA", "ai_customer")
    checkpoint_schema: str = env("CHECKPOINT_SCHEMA", "") or env(
        "APP_DB_SCHEMA", "ai_customer"
    )
    db_pool_max_size: int = int(env("DB_POOL_MAX_SIZE", "10") or "10")
    # 生产建议 false：MemorySaver 重启会丢 Clarify 中断态
    allow_memory_checkpoint: bool = env_bool(
        "ALLOW_MEMORY_CHECKPOINT",
        default=env("ENVIRONMENT", "development") != "production",
    )

    demo_dir: Path = _DEMO_DIR

    @property
    def is_production(self) -> bool:
        return self.environment.lower() == "production"


# 全项目：from config import settings
settings = Settings()


def validate_settings(*, strict: bool | None = None) -> list[str]:
    """
    启动自检。

    strict=None 时：生产严格（缺 PROJECT_ID 直接 fatal），开发只 warning。
    """
    warnings: list[str] = []
    fatal: list[str] = []
    strict = settings.is_production if strict is None else strict

    if not settings.project_id:
        (fatal if strict else warnings).append("未配置 PROJECT_ID")
    if settings.max_tool_loops < 1:
        fatal.append("MAX_TOOL_LOOPS 必须 >= 1")
    if settings.max_clarify_loops < 1:
        fatal.append("MAX_CLARIFY_LOOPS 必须 >= 1")

    for w in warnings:
        logger.warning("config: %s", w)
    if fatal:
        raise RuntimeError("配置校验失败: " + "; ".join(fatal))
    return warnings
