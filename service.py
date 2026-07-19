"""
AI 客服 — 问答编排（召回 + 生成）
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

from config import settings
from llm import DEFAULT_SYSTEM_PROMPT, DashScopeChat, LlmError
from mcp_client import MCPClientError, build_mcp_client
from rag import RagRetriever, RetrievalResult
from store import ChatStore

_PROMPT_CACHE_TTL_SEC = 60.0


@dataclass
class Answer:
    question: str
    answer: str
    retrieval: RetrievalResult
    degraded: bool = False
    error: str | None = None
    sources_summary: list[dict] = field(default_factory=list)
    session_id: str | None = None


class CustomerService:
    def __init__(self) -> None:
        self.settings = settings
        self.client = build_mcp_client()
        self.retriever = RagRetriever(
            self.client,
            project_id=settings.project_id,
            top_k=settings.top_k,
            threshold=settings.search_threshold,
        )
        self.llm = DashScopeChat(
            api_key=settings.dashscope_api_key,
            model=settings.llm_model,
        )
        self.store = ChatStore(settings.sqlite_path)
        self._prompt_cache: tuple[float, str] | None = None

    def resolve_system_prompt(self) -> str:
        """从中台按 PROJECT_ID 拉取 system 提示词；失败则用本地默认。短缓存 60s。"""
        now = time.monotonic()
        if self._prompt_cache is not None:
            ts, cached = self._prompt_cache
            if now - ts < _PROMPT_CACHE_TTL_SEC and cached:
                return cached

        text = DEFAULT_SYSTEM_PROMPT
        try:
            raw = self.client.call_tool(
                "get_project_prompt",
                {"project_id": self.settings.project_id, "name": "system"},
            )
            data = json.loads(raw) if raw else None
            if isinstance(data, dict):
                content = (data.get("content") or "").strip()
                if content:
                    text = content
        except (MCPClientError, json.JSONDecodeError, TypeError, ValueError):
            pass

        self._prompt_cache = (now, text)
        return text

    def ask(
        self,
        question: str,
        *,
        session_id: str | None = None,
        history: list[tuple[str, str]] | None = None,
        persist: bool = True,
    ) -> Answer:
        question = (question or "").strip()
        if not question:
            raise ValueError("问题不能为空")

        sid = session_id
        if persist:
            if not sid:
                sid = self.store.create_session()
            self.store.add_message(sid, "user", question)

        try:
            retrieval = self.retriever.retrieve(question)
        except MCPClientError as exc:
            ans = Answer(
                question=question,
                answer=f"知识库召回失败：{exc}",
                retrieval=RetrievalResult(query=question, raw_text="", context=""),
                degraded=True,
                error=str(exc),
            )
            if persist and sid:
                self.store.add_message(sid, "assistant", ans.answer, trace_id=None)
            return ans

        sources_summary = [
            {
                "filename": c.filename,
                "similarity": c.similarity,
                "preview": c.content[:160],
                "tag": c.sources_tag,
            }
            for c in retrieval.chunks
        ]

        if not self.llm.available:
            # 降级：仅返回召回内容，不调用 LLM
            if retrieval.chunks:
                body = "（未配置 DASHSCOPE_API_KEY，以下为知识库召回结果，未生成客服话术）\n\n"
                body += retrieval.context
            else:
                body = "未配置大模型密钥，且知识库无匹配结果。请配置 DASHSCOPE_API_KEY 并确认项目已上传文档。"
            ans = Answer(
                question=question,
                answer=body,
                retrieval=retrieval,
                degraded=True,
                error="missing DASHSCOPE_API_KEY",
                sources_summary=sources_summary,
            )
        else:
            try:
                text = self.llm.generate(
                    question,
                    retrieval.context,
                    history=history,
                    system_prompt=self.resolve_system_prompt(),
                )
                ans = Answer(
                    question=question,
                    answer=text,
                    retrieval=retrieval,
                    degraded=False,
                    sources_summary=sources_summary,
                )
            except LlmError as exc:
                fallback = (
                    f"大模型生成失败：{exc}\n\n"
                    f"--- 知识库召回 ---\n{retrieval.context or '（无）'}"
                )
                ans = Answer(
                    question=question,
                    answer=fallback,
                    retrieval=retrieval,
                    degraded=True,
                    error=str(exc),
                    sources_summary=sources_summary,
                )

        if persist and sid:
            self.store.add_message(
                sid,
                "assistant",
                ans.answer,
                sources=json.dumps(sources_summary, ensure_ascii=False),
                trace_id=retrieval.trace_id,
            )
            ans.session_id = sid

        return ans
