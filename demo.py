"""
AI 客服 MCP 连接 Demo
=====================

演示外部「AI 客服」项目如何连接已准备好的 MCP 服务，完成：
1. 列出可用工具 (tools/list)
2. 向量搜索知识库 (search_documents)
3. 查看项目统计 (get_project_statistics)
4. 在项目 schema 内建表 / 列表 (create_table / list_tables)

## 前置条件

1. 数据中台已启动 API：`uv run python start_api.py`
2. 已启动 MCP：`uv run python start_mcp.py`
3. 数据中台 `.env` 已配置 MCP_API_KEY（MCP 调 API 用）
4. `mcp_service` 账号需 `is_superuser=true`（MCP 代理全部项目）
5. 本目录 `.env` 已配置 PROJECT_ID、可选 MCP_TCP_SECRET

## 运行

```bash
# 在项目根目录
cp models/ai_customer/.env.example models/ai_customer/.env
# 编辑 .env 填入 PROJECT_ID 等

uv run python models/ai_customer/demo.py
uv run python models/ai_customer/demo.py --tool tables
```

可选参数：
```bash
uv run python models/ai_customer/demo.py --query "如何联系客服"
uv run python models/ai_customer/demo.py --tool list
```
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# 将 Demo 目录加入 path，便于单独拷贝到其他项目时调整
_DEMO_DIR = Path(__file__).resolve().parent
if str(_DEMO_DIR) not in sys.path:
    sys.path.insert(0, str(_DEMO_DIR))

from dotenv import load_dotenv

from mcp_client import MCPClientError, MCPTcpClient

# 优先加载 Demo 专用 .env，再 fallback 项目根 .env
load_dotenv(_DEMO_DIR / ".env")
# 可选：继承项目根 .env 中的 MCP_TCP_SECRET 等
if len(_DEMO_DIR.parents) >= 3:
    load_dotenv(_DEMO_DIR.parents[2] / ".env")


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def build_client() -> MCPTcpClient:
    """从环境变量构建 MCP 客户端。"""
    return MCPTcpClient(
        host=_env("MCP_HOST", "127.0.0.1"),
        port=int(_env("MCP_PORT", "8765")),
        auth_token=_env("MCP_TCP_SECRET"),
        timeout=float(_env("MCP_TIMEOUT", "120")),
    )


def print_banner() -> None:
    print("=" * 60)
    print("  AI 客服 × 数据中台 MCP 连接 Demo")
    print("=" * 60)
    print(f"  MCP 地址: {_env('MCP_HOST', '127.0.0.1')}:{_env('MCP_PORT', '8765')}")
    print(f"  项目 ID:  {_env('PROJECT_ID') or '(未设置，搜索将不限定项目)'}")
    print(f"  TCP 认证: {'已配置 MCP_TCP_SECRET' if _env('MCP_TCP_SECRET') else '未配置（开发环境本地连接）'}")
    print("=" * 60)
    print()


def demo_list_tools(client: MCPTcpClient) -> None:
    """步骤 1：列出 MCP 注册的工具。"""
    print("[1/4] tools/list — 可用工具")
    print("-" * 40)
    tools = client.list_tools()
    if not tools:
        print("  (无工具返回，请检查 MCP 与 API 是否正常)")
        return
    for i, tool in enumerate(tools, 1):
        name = tool.get("name", "?")
        desc = tool.get("description", "")
        print(f"  {i}. {name}")
        print(f"     {desc}")
    print()


def demo_search(client: MCPTcpClient, query: str, project_id: str, top_k: int) -> None:
    """步骤 2：语义搜索文档片段（RAG 召回）。"""
    import uuid

    print(f"[2/4] search_documents — query: 「{query}」")
    print("-" * 40)
    params: dict = {"query": query, "top_k": top_k}
    if project_id:
        params["project_id"] = project_id
    # 演示：客户端可自带 trace_id，便于在 GET /api/traces/{id} 还原链路
    demo_trace = str(uuid.uuid4())
    text = client.call_tool("search_documents", params, trace_id=demo_trace)
    print(text)
    print(f"  trace_id: {client.last_trace_id or demo_trace}")
    print(f"  查询链路: GET /api/traces/{client.last_trace_id or demo_trace}")
    print()


def demo_project_stats(client: MCPTcpClient, project_id: str) -> None:
    """步骤 3：查看项目文档/向量统计。"""
    if not project_id:
        print("[3/4] get_project_statistics — 跳过（未设置 PROJECT_ID）")
        print()
        return
    print("[3/4] get_project_statistics")
    print("-" * 40)
    text = client.call_tool("get_project_statistics", {"project_id": project_id})
    print(text)
    print()


def demo_tables(client: MCPTcpClient, project_id: str, *, create_sample: bool = True) -> None:
    """步骤 4：在项目 schema 内 list / create 样例表。"""
    if not project_id:
        print("[4/4] tables — 跳过（未设置 PROJECT_ID）")
        print()
        return

    print("[4/4] list_tables / create_table")
    print("-" * 40)
    print(client.call_tool("list_tables", {"project_id": project_id}))
    print()

    if create_sample:
        columns = [
            {"name": "id", "type": "uuid", "primary_key": True, "default": "gen_random_uuid()"},
            {"name": "session_id", "type": "varchar(100)", "nullable": False},
            {"name": "question", "type": "text", "nullable": False},
            {"name": "answer", "type": "text", "nullable": True},
            {"name": "created_at", "type": "timestamptz", "default": "now()"},
        ]
        print(
            client.call_tool(
                "create_table",
                {
                    "project_id": project_id,
                    "table_name": "customer_service_conversations",
                    "columns": columns,
                },
            )
        )
        print()
        print(
            client.call_tool(
                "describe_table",
                {
                    "project_id": project_id,
                    "table_name": "customer_service_conversations",
                },
            )
        )
        print()
        print(client.call_tool("list_tables", {"project_id": project_id}))
        print()


def main() -> None:
    parser = argparse.ArgumentParser(description="AI 客服 MCP 连接演示")
    parser.add_argument(
        "--query",
        default=_env("DEMO_SEARCH_QUERY", "常见问题"),
        help="搜索关键词（默认读 DEMO_SEARCH_QUERY 或「常见问题」）",
    )
    parser.add_argument(
        "--project-id",
        default=_env("PROJECT_ID"),
        help="限定搜索的项目 UUID",
    )
    parser.add_argument("--top-k", type=int, default=int(_env("DEMO_TOP_K", "5")))
    parser.add_argument(
        "--tool",
        choices=("all", "list", "search", "stats", "tables"),
        default="all",
        help="只运行某一步（默认全部）",
    )
    args = parser.parse_args()

    print_banner()
    client = build_client()

    try:
        if args.tool in ("all", "list"):
            demo_list_tools(client)
        if args.tool in ("all", "search"):
            demo_search(client, args.query, args.project_id, args.top_k)
        if args.tool in ("all", "stats"):
            demo_project_stats(client, args.project_id)
        if args.tool in ("all", "tables"):
            demo_tables(client, args.project_id, create_sample=True)
    except MCPClientError as exc:
        print(f"\n❌ Demo 失败: {exc}", file=sys.stderr)
        print("\n排查清单:", file=sys.stderr)
        print("  1. API 是否运行？ curl http://127.0.0.1:8000/health/ready", file=sys.stderr)
        print("  2. MCP 是否运行？ uv run python start_mcp.py", file=sys.stderr)
        print("  3. 数据中台 .env 是否配置 MCP_API_KEY？", file=sys.stderr)
        print("  4. mcp_service 是否 is_superuser=true？", file=sys.stderr)
        print("  5. 若配置了 MCP_TCP_SECRET，Demo .env 需填相同值", file=sys.stderr)
        sys.exit(1)

    print("✅ Demo 完成。")
    print("\n完整客服 UI：uv run streamlit run models/ai_customer/app.py")
    print("终端多轮：  uv run python models/ai_customer/cli_chat.py")


if __name__ == "__main__":
    main()
