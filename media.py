"""
素材召回（media.py）— 经 MCP search_media
========================================

客服不直连素材存储，只调中台 MCP。
返回 attachments 供 Answer / UI 渲染。
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from mcp_client import MCPClientError

logger = logging.getLogger(__name__)


@dataclass
class MediaAttachment:
    asset_id: str
    type: str  # image | video
    url: str
    thumb_url: str | None = None
    title: str = ""
    caption: str = ""
    score: float | None = None
    when_to_use: str = ""
    when_not_to_use: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "asset_id": self.asset_id,
            "type": self.type,
            "url": self.url,
            "thumb_url": self.thumb_url,
            "title": self.title,
            "caption": self.caption,
            "score": self.score,
            "when_to_use": self.when_to_use,
            "when_not_to_use": self.when_not_to_use,
        }


@dataclass
class MediaRetrievalResult:
    query: str
    raw_text: str
    attachments: list[MediaAttachment] = field(default_factory=list)
    context: str = ""
    trace_id: str | None = None


class MediaRetriever:
    def __init__(
        self,
        client: Any,
        *,
        project_id: str,
        top_k: int = 5,
        threshold: float = 0.45,
    ) -> None:
        self.client = client
        self.project_id = project_id
        self.top_k = top_k
        self.threshold = threshold

    def retrieve(
        self,
        query: str,
        *,
        media_type: str | None = None,
        scene: str | None = None,
    ) -> MediaRetrievalResult:
        params: dict[str, Any] = {
            "query": query,
            "top_k": self.top_k,
            "threshold": self.threshold,
        }
        if self.project_id:
            params["project_id"] = self.project_id
        if media_type:
            params["media_type"] = media_type
        if scene:
            params["scene"] = scene

        trace_id = str(uuid.uuid4())
        try:
            raw = self.client.call_tool("search_media", params, trace_id=trace_id)
        except MCPClientError:
            raise

        attachments: list[MediaAttachment] = []
        try:
            data = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            data = {}

        results = data.get("results") if isinstance(data, dict) else None
        if isinstance(results, list):
            for item in results:
                if not isinstance(item, dict):
                    continue
                attachments.append(
                    MediaAttachment(
                        asset_id=str(item.get("asset_id") or ""),
                        type=str(item.get("media_type") or "image"),
                        url=str(item.get("content_url") or ""),
                        thumb_url=item.get("thumb_url"),
                        title=str(item.get("title") or ""),
                        caption=str(item.get("caption") or ""),
                        score=float(item["score"])
                        if isinstance(item.get("score"), (int, float))
                        else None,
                        when_to_use=str(item.get("when_to_use") or ""),
                        when_not_to_use=str(item.get("when_not_to_use") or ""),
                    )
                )

        if attachments:
            blocks = []
            for idx, a in enumerate(attachments, 1):
                score = f"{a.score:.3f}" if a.score is not None else "-"
                blocks.append(
                    f"[{idx}] 素材: {a.title} | 类型: {a.type} | score: {score}\n"
                    f"说明: {a.caption}\n"
                    f"使用: {a.when_to_use}\n"
                    f"不宜: {a.when_not_to_use}\n"
                    f"asset_id: {a.asset_id}"
                )
            context = "\n\n".join(blocks)
        else:
            context = ""

        return MediaRetrievalResult(
            query=query,
            raw_text=raw or "",
            attachments=attachments,
            context=context,
            trace_id=getattr(self.client, "last_trace_id", None) or trace_id,
        )
