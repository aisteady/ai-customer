"""
AI 客服 — 终端多轮问答
======================

uv run python models/ai_customer/cli_chat.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_DIR = Path(__file__).resolve().parent
if str(_DIR) not in sys.path:
    sys.path.insert(0, str(_DIR))

from config import settings
from mcp_client import MCPClientError
from service import CustomerService


def main() -> None:
    print("=" * 60)
    print("  AI 客服（终端模式）")
    print("=" * 60)
    print(f"  MCP: {settings.mcp_host}:{settings.mcp_port}")
    print(f"  PROJECT_ID: {settings.project_id or '(未设置)'}")
    print(f"  LLM: {settings.llm_model} | Key: {'有' if settings.dashscope_api_key else '无'}")
    print("  输入问题回车；输入 quit / exit 退出")
    print("=" * 60)

    svc = CustomerService()
    session_id = svc.store.create_session(title="CLI会话")
    history: list[tuple[str, str]] = []

    while True:
        try:
            q = input("\n你: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见。")
            break
        if not q:
            continue
        if q.lower() in ("quit", "exit", "q"):
            print("再见。")
            break

        try:
            ans = svc.ask(q, session_id=session_id, history=history, persist=True)
        except (MCPClientError, ValueError) as exc:
            print(f"错误: {exc}")
            continue

        print(f"\n客服: {ans.answer}")
        if ans.retrieval.trace_id:
            print(f"  (trace_id={ans.retrieval.trace_id})")
        if ans.sources_summary:
            print("  引用:")
            for i, s in enumerate(ans.sources_summary, 1):
                print(f"    {i}. {s.get('filename')} sim={s.get('similarity')}")

        history.append(("user", q))
        history.append(("assistant", ans.answer))


if __name__ == "__main__":
    main()
