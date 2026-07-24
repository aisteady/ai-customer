"""
客服工具集（tools.py）
======================

- rag_search                 → MCP search_documents
- search_media               → MCP search_media（多模态素材）
- process_config_recommend   → HTTP 调工艺助手 /api/v1/recommend（未配置 URL 则 stub）
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from config import settings
from media import MediaRetriever, MediaRetrievalResult
from rag import RagRetriever, RetrievalResult

logger = logging.getLogger(__name__)

AVAILABLE_TOOLS = ("rag_search", "search_media", "process_config_recommend")


def process_config_recommend_stub(
    query: str,
    *,
    extras: str = "",
    slots: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """本地模拟（未配置 PROCESS_CONFIG_URL 时使用）。"""
    q = (query or "").strip()
    extra = (extras or "").strip()
    body = (
        "【工艺配置推荐助手 · 模拟结果】\n"
        f"查询：{q or '（空）'}\n"
    )
    if extra:
        body += f"补充信息：{extra}\n"
    if slots:
        body += f"已填槽位：{slots}\n"
    body += (
        "\n【最终配置方案 · 模拟】\n"
        "1. 主机：根据物料硬度与目标细度，建议优先评估立磨/球磨方案（示意）。\n"
        "2. 配套：分级机、除尘、输送；若进料粒度偏大可考虑前置破碎（示意）。\n"
        "3. 说明：本方案为示意配置。请配置 PROCESS_CONFIG_URL 对接真实工艺助手。\n"
    )
    return {
        "tool": "process_config_recommend",
        "ok": True,
        "text": body,
        "stub": True,
        "slots_partial": dict(slots or {}),
    }


def process_config_recommend_live(
    query: str,
    *,
    extras: str = "",
    slots: dict[str, Any] | None = None,
    session_id: str = "",
) -> dict[str, Any]:
    """
    调用 ai_quotation POST /api/v1/recommend。

    缺五要素时返回 ok=False + missing，由客服 Clarify Loop 追问。
    """
    url = (settings.process_config_url or "").rstrip("/")
    if not url.endswith("/recommend"):
        # 允许只配到 host:port 或 /api/v1
        if url.endswith("/api/v1"):
            url = url + "/recommend"
        else:
            url = url + "/api/v1/recommend"

    headers = {"Content-Type": "application/json"}
    token = (settings.process_config_token or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    payload = {
        "query": query or "",
        "extras": extras or "",
        "slots": slots or {},
        "session_id": session_id or "",
    }
    try:
        with httpx.Client(timeout=settings.process_config_timeout) as client:
            resp = client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        logger.warning("process_config HTTP 失败: %s", exc)
        return {
            "tool": "process_config_recommend",
            "ok": False,
            "text": f"工艺配置助手调用失败：{exc}",
            "error": "http_error",
            "sources": [],
            "stub": False,
        }

    if not isinstance(data, dict):
        return {
            "tool": "process_config_recommend",
            "ok": False,
            "text": "工艺配置助手返回格式异常",
            "error": "bad_response",
            "stub": False,
        }

    ok = bool(data.get("ok"))
    missing = list(data.get("missing") or [])
    missing_labels = list(data.get("missing_labels") or [])
    clarify_q = str(data.get("clarify_question") or "").strip()
    slots_partial = data.get("slots_partial") if isinstance(data.get("slots_partial"), dict) else {}
    proposal = str(data.get("proposal_text") or "").strip()
    structured = data.get("structured")

    if not ok and data.get("error") == "missing_required_slots":
        labels = missing_labels or missing
        text = clarify_q or (
            "生成配置清单前还需确认：" + "、".join(str(x) for x in labels) + "。"
        )
        return {
            "tool": "process_config_recommend",
            "ok": False,
            "text": text,
            "error": "missing_required_slots",
            "missing": missing,
            "missing_labels": missing_labels,
            "clarify_question": text,
            "slots_partial": slots_partial,
            "structured": None,
            "stub": False,
        }

    if not ok:
        return {
            "tool": "process_config_recommend",
            "ok": False,
            "text": proposal or str(data.get("error") or "工艺配置失败"),
            "error": str(data.get("error") or "recommend_failed"),
            "slots_partial": slots_partial,
            "stub": False,
        }

    return {
        "tool": "process_config_recommend",
        "ok": True,
        "text": proposal or "（配置方案为空）",
        "structured": structured,
        "slots_partial": slots_partial,
        "sources": [],
        "stub": False,
    }


class CustomerTools:
    """给图节点用的工具门面。"""

    def __init__(
        self,
        retriever: RagRetriever | None,
        media_retriever: MediaRetriever | None = None,
    ) -> None:
        self.retriever = retriever
        self.media_retriever = media_retriever

    def rag_search(self, query: str) -> dict[str, Any]:
        if not self.retriever:
            return {
                "tool": "rag_search",
                "ok": False,
                "text": "RAG 未就绪（缺少 PROJECT_ID 或 MCP）",
                "sources": [],
                "attachments": [],
                "trace_id": None,
            }
        try:
            result: RetrievalResult = self.retriever.retrieve(query)
        except Exception as exc:
            logger.warning("rag_search 失败: %s", exc)
            return {
                "tool": "rag_search",
                "ok": False,
                "text": f"知识库召回失败：{exc}",
                "sources": [],
                "attachments": [],
                "trace_id": None,
            }

        sources = [
            {
                "filename": c.filename,
                "similarity": c.similarity,
                "preview": c.content[:160],
                "tag": c.sources_tag,
            }
            for c in result.chunks
        ]
        text = result.context or "（知识库无匹配片段）"
        return {
            "tool": "rag_search",
            "ok": bool(result.chunks),
            "text": text,
            "sources": sources,
            "attachments": [],
            "trace_id": result.trace_id,
            "raw_text": result.raw_text,
        }

    def search_media(self, query: str) -> dict[str, Any]:
        if not self.media_retriever:
            return {
                "tool": "search_media",
                "ok": False,
                "text": "素材检索未就绪（缺少 PROJECT_ID 或 MCP）",
                "sources": [],
                "attachments": [],
                "trace_id": None,
            }
        try:
            result: MediaRetrievalResult = self.media_retriever.retrieve(query)
        except Exception as exc:
            logger.warning("search_media 失败: %s", exc)
            return {
                "tool": "search_media",
                "ok": False,
                "text": f"素材召回失败：{exc}",
                "sources": [],
                "attachments": [],
                "trace_id": None,
            }

        attachments = [a.to_dict() for a in result.attachments[:3]]
        sources = [
            {
                "kind": "media",
                "filename": a.get("title") or a.get("asset_id"),
                "similarity": a.get("score"),
                "preview": a.get("caption") or "",
                "asset_id": a.get("asset_id"),
                "type": a.get("type"),
            }
            for a in attachments
        ]
        text = result.context or "（素材库无匹配）"
        return {
            "tool": "search_media",
            "ok": bool(attachments),
            "text": text,
            "sources": sources,
            "attachments": attachments,
            "trace_id": result.trace_id,
            "raw_text": result.raw_text,
        }

    def run(
        self,
        name: str,
        query: str,
        *,
        extras: str = "",
        slots: dict[str, Any] | None = None,
        session_id: str = "",
    ) -> dict[str, Any]:
        if name == "rag_search":
            return self.rag_search(query)
        if name == "search_media":
            return self.search_media(query)
        if name == "process_config_recommend":
            if settings.process_config_url:
                return process_config_recommend_live(
                    query,
                    extras=extras,
                    slots=slots,
                    session_id=session_id,
                )
            return process_config_recommend_stub(
                query, extras=extras, slots=slots
            )
        return {
            "tool": name,
            "ok": False,
            "text": f"未知工具：{name}",
            "sources": [],
            "attachments": [],
        }
