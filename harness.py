"""
线上 Harness（harness.py）
==========================

学习要点
--------
什么是 Harness？
  包在「业务图 / Agent」外面的一层：**守卫 + 审计**。
  业务逻辑仍在 LangGraph（见 graph/）；Harness 负责「别跑飞、留痕迹」。

与 ai_quotation 的差异：
  询报价还有后台 HarnessLoop 扫「待人审」积压；
  客服没有人审工单模型，本文件**只有** CustomerHarness，不做定时扫描。

图内还有两类 Loop（不要和 Harness 混为一谈）：
  - Clarify Loop：信息不足 → interrupt 追问用户 → 再规划（nodes.clarify）
  - Tool Loop：调工具 → judge → 不满意再调（最多 MAX_TOOL_LOOPS 次）

读代码顺序：guard_* → emit → wrap_run；再看 service.ask 如何调用 wrap_run。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable

from config import settings
from store import ChatStore

logger = logging.getLogger(__name__)


class CustomerHarness:
    """
    运行时护栏与事件埋点。

    典型用法（见 service.py）：
        return self.harness.wrap_run("ask", _run, session_id=sid)
    """

    def __init__(self, store: ChatStore) -> None:
        self.store = store
        # 与图内 clarify / tool 节点共用同一上限（改 .env 即可调参）
        self.max_clarify_loops = settings.max_clarify_loops
        self.max_tool_loops = settings.max_tool_loops

    def guard_clarify_round(self, clarify_round: int) -> tuple[bool, str]:
        """
        追问轮数硬顶。

        返回 (是否允许继续追问, 说明文案)。
        超过上限后图应停止 interrupt，带着已有 extras 去调工具 / 兜底。
        """
        if clarify_round > self.max_clarify_loops:
            return (
                False,
                f"已超过最大追问轮数 ({self.max_clarify_loops})，将基于现有信息继续作答。",
            )
        return True, ""

    def guard_tool_round(self, tool_round: int) -> tuple[bool, str]:
        """
        工具调用轮数硬顶（见 MAX_TOOL_LOOPS / settings.max_tool_loops）。

        超过后不应再调工具，应走 fallback_answer。
        """
        if tool_round > self.max_tool_loops:
            return (
                False,
                f"已超过最大工具调用轮数 ({self.max_tool_loops})，转入兜底生成。",
            )
        return True, ""

    def emit(
        self,
        event_type: str,
        *,
        session_id: str | None = None,
        **payload: Any,
    ) -> None:
        """写 harness_events；失败只打日志，不打断主流程。"""
        try:
            self.store.log_harness_event(
                event_type, session_id=session_id, payload=payload
            )
        except Exception as exc:
            logger.warning("harness 事件写入失败: %s", exc)

    def wrap_run(
        self,
        name: str,
        fn: Callable[[], Any],
        *,
        session_id: str | None = None,
    ) -> Any:
        """
        包装一次业务调用：记 start → 执行 → 记 ok/error + 耗时。

        service.ask / resume_clarify 都走这里，便于事后按 session 复盘。
        """
        self.emit("run_start", session_id=session_id, name=name)
        t0 = time.time()
        try:
            result = fn()
            self.emit(
                "run_ok",
                session_id=session_id,
                name=name,
                elapsed_ms=int((time.time() - t0) * 1000),
            )
            return result
        except Exception as exc:
            self.emit(
                "run_error",
                session_id=session_id,
                name=name,
                error=str(exc),
                elapsed_ms=int((time.time() - t0) * 1000),
            )
            raise
