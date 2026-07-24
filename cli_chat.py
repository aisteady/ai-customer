"""
终端多轮问答（cli_chat.py）
==========================

学习要点
--------
与 app.py 同一套 CustomerService，便于无 UI 时联调 MCP / 图逻辑。

快捷命令：
  /customer  /employee     — 切换 role_hint
  /auto on|off             — 是否 LLM 自动意图
  quit / exit / q / /quit  — 退出

Clarify：pending_clarify=True 时下一句走 resume_clarify，与 Streamlit 一致。

运行：
  cd models/ai_customer && uv run python cli_chat.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_DIR = Path(__file__).resolve().parent
if str(_DIR) not in sys.path:
    sys.path.insert(0, str(_DIR))

from config import settings
from service import CustomerService


def main() -> None:
    print("=" * 60)
    print("  AI 客服（终端模式 · Agent + Loop + Harness）")
    print("=" * 60)
    print(f"  MCP: {settings.mcp_url}")
    print(f"  PROJECT_ID: {settings.project_id or '(未设置)'}")
    print(f"  LLM model override: {settings.llm_model or '(中台项目配置)'}")
    print("  命令: /customer /employee /auto on|off /quit")
    print("=" * 60)

    svc = CustomerService()
    session_id = svc.store.create_session(title="CLI会话")
    thread_id = session_id
    history: list[tuple[str, str]] = []
    role_hint = "customer"
    auto_intent = False
    pending_clarify = False

    while True:
        try:
            q = input("\n你: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见。")
            break
        if not q:
            continue
        low = q.lower()
        if low in ("quit", "exit", "q", "/quit"):
            print("再见。")
            break
        if low == "/customer":
            role_hint = "customer"
            print("已切换为客户身份")
            continue
        if low == "/employee":
            role_hint = "employee"
            print("已切换为员工身份")
            continue
        if low in ("/auto on", "/auto"):
            auto_intent = True
            print("已开启自动意图识别")
            continue
        if low == "/auto off":
            auto_intent = False
            print("已关闭自动意图识别")
            continue

        try:
            if pending_clarify:
                ans = svc.resume_clarify(thread_id, q)
            else:
                ans = svc.ask(
                    q,
                    session_id=session_id,
                    thread_id=thread_id,
                    history=history,
                    role_hint=role_hint,
                    auto_intent=auto_intent,
                    persist=True,
                )
        except (ValueError, RuntimeError) as exc:
            print(f"错误: {exc}")
            continue

        if ans.thread_id:
            thread_id = ans.thread_id
        pending_clarify = bool(ans.interrupted)

        print(f"\n客服: {ans.answer}")
        print(
            f"  [role={ans.role} tool={ans.tool_round} "
            f"clarify={ans.clarify_round}"
            f"{' interrupted' if ans.interrupted else ''}"
            f"{' degraded' if ans.degraded else ''}]"
        )
        if ans.trace_id:
            print(f"  (trace_id={ans.trace_id})")
        if ans.sources_summary:
            print("  引用:")
            for i, s in enumerate(ans.sources_summary, 1):
                print(f"    {i}. {s.get('filename')} sim={s.get('similarity')}")

        history.append(("user", q))
        history.append(("assistant", ans.answer))


if __name__ == "__main__":
    main()
