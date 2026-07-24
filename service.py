"""
AI 客服 — 编排门面（service.py）
================================

学习要点
--------
UI/CLI **只应依赖本文件**，不要直接调 graph 节点。

对外 API：
  ask(question, role_hint=..., auto_intent=...)  — 新问题 / 新一轮
  resume_clarify(thread_id, user_reply)            — Clarify interrupt 后的补充
  get_state(thread_id)                             — 调试或 UI 探测中断态

内部装配顺序（__init__）：
  MCP client → ChatStore → Harness → McpChat → RagRetriever/Tools → LangGraph

thread_id 与 session_id：
  session_id = 业务会话（messages 表）
  thread_id  = LangGraph checkpoint 键（interrupt 恢复必须同一个）
  默认两者相同；加载历史会话时从 sessions.thread_id 读回。

读代码：ask → wrap_run → graph.invoke → _to_answer；
再对照 FLOW_ANALYSIS.md 看员工/客户两条路径。
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Sequence

from config import settings, validate_settings
from graph.build import build_graph
from harness import CustomerHarness
from llm import DEFAULT_SYSTEM_PROMPT, McpChat
from mcp_client import build_mcp_client
from media import MediaRetriever
from rag import RagRetriever
from store import ChatStore
from tools import CustomerTools

logger = logging.getLogger(__name__)

_PROMPT_CACHE_TTL_SEC = 60.0


@dataclass
class Answer:
    """
    一次问答（或一次 Clarify 追问）的结果，供 UI 渲染。

    interrupted=True 时：answer 通常是追问文案，下一轮应调 resume_clarify。
    """

    question: str
    answer: str
    role: str = "customer"
    degraded: bool = False
    error: str | None = None
    sources_summary: list[dict] = field(default_factory=list)
    attachments: list[dict] = field(default_factory=list)
    session_id: str | None = None
    thread_id: str | None = None
    interrupted: bool = False
    interrupt_payload: dict[str, Any] | None = None
    trace_id: str | None = None
    status: str = "done"
    tool_round: int = 0
    clarify_round: int = 0

    @property
    def retrieval(self) -> Any:
        """兼容旧 UI 读 result.retrieval.trace_id 的写法。"""

        class _R:
            pass

        r = _R()
        r.trace_id = self.trace_id
        r.context = ""
        r.chunks = []
        r.query = self.question
        r.raw_text = ""
        return r


class CustomerService:
    """客服应用唯一业务入口。"""

    def __init__(self) -> None:
        validate_settings(strict=False)
        self.settings = settings
        self.client = build_mcp_client()
        self.store = ChatStore()
        self.harness = CustomerHarness(self.store)
        self.llm = McpChat(
            project_id=settings.project_id,
            model=settings.llm_model or None,
            client=self.client,
        )
        # 无 PROJECT_ID 时仍可起服务；RAG 工具会返回明确降级文案
        retriever = None
        media_retriever = None
        if settings.project_id:
            retriever = RagRetriever(
                self.client,
                project_id=settings.project_id,
                top_k=settings.top_k,
                threshold=settings.search_threshold,
            )
        media_pid = (settings.media_project_id or settings.project_id or "").strip()
        if media_pid:
            media_retriever = MediaRetriever(
                self.client,
                project_id=media_pid,
                top_k=settings.top_k,
                threshold=settings.media_search_threshold,
            )
        self.tools = CustomerTools(retriever, media_retriever)
        self._prompt_cache: tuple[float, str] | None = None
        self.graph = build_graph(
            self.store,
            self.harness,
            llm=self.llm,
            tools=self.tools,
            employee_system=self.resolve_system_prompt(),
        )

    def resolve_system_prompt(self) -> str:
        """
        员工路径 system：优先中台「提示词管理」；失败用本地 EMPLOYEE_SYSTEM。
        短缓存 60s，避免每句都打 MCP。
        """
        now = time.monotonic()
        if self._prompt_cache is not None:
            ts, cached = self._prompt_cache
            if now - ts < _PROMPT_CACHE_TTL_SEC and cached:
                return cached

        text = DEFAULT_SYSTEM_PROMPT
        if self.settings.project_id:
            try:
                import json

                raw = self.client.call_tool(
                    "get_project_prompt",
                    {"project_id": self.settings.project_id, "name": "system"},
                )
                data = json.loads(raw) if raw else None
                if isinstance(data, dict):
                    content = (data.get("content") or "").strip()
                    if content:
                        text = content
            except Exception:
                pass

        self._prompt_cache = (now, text)
        return text

    def ask(
        self,
        question: str,
        *,
        session_id: str | None = None,
        thread_id: str | None = None,
        history: Sequence[tuple[str, str]] | None = None,
        role_hint: str = "customer",
        auto_intent: bool = False,
        persist: bool = True,
    ) -> Answer:
        """
        发起一轮问答（从 route_intent 跑到结束或 Clarify interrupt）。

        role_hint / auto_intent：见 nodes.route_intent。
        persist=False：不创建会话、不写库（适合探测；生产 UI 用 True）。
        """
        question = (question or "").strip()
        if not question:
            raise ValueError("问题不能为空")

        hint = (role_hint or "customer").strip().lower()
        if hint not in ("customer", "employee"):
            hint = "customer"

        sid = session_id
        if persist:
            if not sid:
                sid = self.store.create_session()
        tid = thread_id or sid or str(uuid.uuid4())
        if persist and sid:
            self.store.update_session_meta(sid, role=hint, thread_id=tid)

        # TypedDict history 用 list[list[str]]，避免 tuple 序列化问题
        hist = [[r, c] for r, c in (history or []) if r in ("user", "assistant") and c]

        def _run() -> Answer:
            # configurable.thread_id 必须与 resume 时一致
            config = {"configurable": {"thread_id": tid}}
            self.graph.invoke(
                {
                    "session_id": sid or "",
                    "thread_id": tid,
                    "question": question,
                    "user_reply": "",
                    "extras": "",
                    "role_hint": hint,
                    "auto_intent": auto_intent,
                    "tool_round": 0,
                    "clarify_round": 0,
                    "tool_results": [],
                    "sources": [],
                    "process_slots": {},
                    "need_slot_clarify": False,
                    "history": hist,
                    "status": "running",
                },
                config=config,
            )
            return self._to_answer(tid, question=question, session_id=sid)

        return self.harness.wrap_run("ask", _run, session_id=sid)

    def resume_clarify(self, thread_id: str, user_reply: str) -> Answer:
        """
        恢复 Clarify interrupt。

        对应 LangGraph：Command(resume={"user_reply": ...})
        会成为 nodes.clarify 里 interrupt() 的返回值。
        """
        if not (user_reply or "").strip():
            raise ValueError("补充内容不能为空")

        def _run() -> Answer:
            from langgraph.types import Command

            config = {"configurable": {"thread_id": thread_id}}
            self.graph.invoke(
                Command(resume={"user_reply": user_reply.strip()}),
                config=config,
            )
            return self._to_answer(thread_id)

        return self.harness.wrap_run("resume_clarify", _run)

    def get_state(self, thread_id: str) -> dict[str, Any]:
        """读取图快照：是否中断、interrupt payload、当前 values。"""
        config = {"configurable": {"thread_id": thread_id}}
        snap = self.graph.get_state(config)
        values = dict(snap.values or {})
        interrupted = bool(snap.next)
        payload = None

        def _take_interrupt(obj: Any) -> Any:
            if obj is None:
                return None
            if hasattr(obj, "value"):
                return getattr(obj, "value")
            return obj

        if snap.tasks:
            for t in snap.tasks:
                ints = getattr(t, "interrupts", None) or ()
                if ints:
                    payload = _take_interrupt(ints[0])
                    break
        # 部分版本把 interrupts 挂在 snapshot 上
        if payload is None:
            root_ints = getattr(snap, "interrupts", None) or ()
            if root_ints:
                payload = _take_interrupt(root_ints[0])

        return {
            "thread_id": thread_id,
            "interrupted": interrupted,
            "next": list(snap.next or []),
            "values": values,
            "interrupt_payload": payload,
        }

    def _to_answer(
        self,
        thread_id: str,
        *,
        question: str | None = None,
        session_id: str | None = None,
    ) -> Answer:
        """把 graph state 收成 UI 友好的 Answer。"""
        state = self.get_state(thread_id)
        values = state.get("values") or {}
        q = question or values.get("question") or ""
        interrupted = bool(state.get("interrupted"))
        payload = state.get("interrupt_payload")
        answer_text = values.get("answer") or ""
        # 中断时优先展示追问文案（勿回落到旧的配置草稿）
        if interrupted:
            if isinstance(payload, dict):
                answer_text = str(
                    payload.get("question")
                    or values.get("lead_ask")
                    or answer_text
                )
            elif values.get("lead_ask"):
                answer_text = str(values.get("lead_ask"))
            elif values.get("status") == "need_clarify" and values.get("answer"):
                answer_text = str(values.get("answer"))

        interrupt_type = ""
        if isinstance(payload, dict):
            interrupt_type = str(payload.get("type") or "")
        elif interrupted and values.get("lead_phase") == "collect":
            interrupt_type = "quote_contact"

        return Answer(
            question=q,
            answer=answer_text or ("请补充信息。" if interrupted else ""),
            role=str(values.get("role") or values.get("role_hint") or "customer"),
            degraded=bool(values.get("degraded")),
            error=values.get("error") or None,
            sources_summary=list(values.get("sources") or []),
            attachments=list(values.get("attachments") or []),
            session_id=session_id or values.get("session_id") or None,
            thread_id=thread_id,
            interrupted=interrupted,
            interrupt_payload=(
                payload
                if isinstance(payload, dict)
                else (
                    {"type": interrupt_type, "question": answer_text}
                    if interrupted and interrupt_type
                    else None
                )
            ),
            trace_id=values.get("trace_id") or None,
            status=str(
                values.get("status")
                or ("need_clarify" if interrupted else "done")
            ),
            tool_round=int(values.get("tool_round") or 0),
            clarify_round=int(values.get("clarify_round") or 0),
        )
