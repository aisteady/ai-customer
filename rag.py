"""
AI 客服 — MCP 知识库召回（RAG）
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from mcp_client import MCPClientError


@dataclass
class RetrievedChunk:
    filename: str
    similarity: float | None
    content: str
    sources_tag: str = ""


@dataclass
class RetrievalResult:
    query: str
    raw_text: str
    chunks: list[RetrievedChunk] = field(default_factory=list)
    context: str = ""
    trace_id: str | None = None


_ITEM_RE = re.compile(
    r"^\s*(\d+)\.\s*\[([^\]]*)\]\s*相似度\s*([0-9.]+)(?:\s*\[([^\]]*)\])?\s*$",
    re.MULTILINE,
)


def parse_search_result(text: str) -> list[RetrievedChunk]:
    """解析 MCP search_documents 返回的文本列表。"""
    if not text or "未找到" in text:
        return []

    lines = text.splitlines()
    chunks: list[RetrievedChunk] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        m = _ITEM_RE.match(line.strip())
        if m:
            filename = m.group(2).strip()
            try:
                sim = float(m.group(3))
            except ValueError:
                sim = None
            tag = (m.group(4) or "").strip()
            content_parts: list[str] = []
            i += 1
            while i < len(lines) and not _ITEM_RE.match(lines[i].strip()):
                content_parts.append(lines[i].strip())
                i += 1
            content = "\n".join(p for p in content_parts if p).rstrip(".")
            if content.endswith("..."):
                content = content[:-3].rstrip()
            chunks.append(
                RetrievedChunk(
                    filename=filename or "未知",
                    similarity=sim,
                    content=content,
                    sources_tag=tag,
                )
            )
            continue
        i += 1
    return chunks


class RagRetriever:
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

    def retrieve(self, query: str) -> RetrievalResult:
        params: dict = {
            "query": query,
            "top_k": self.top_k,
            "threshold": self.threshold,
        }
        if self.project_id:
            params["project_id"] = self.project_id

        trace_id = str(uuid.uuid4())
        try:
            raw = self.client.call_tool("search_documents", params, trace_id=trace_id)
        except MCPClientError:
            raise

        chunks = parse_search_result(raw)
        if chunks:
            blocks = []
            for idx, c in enumerate(chunks, 1):
                sim = f"{c.similarity:.2f}" if c.similarity is not None else "-"
                blocks.append(
                    f"[{idx}] 来源文件: {c.filename} | 相似度: {sim}\n{c.content}"
                )
            context = "\n\n".join(blocks)
        else:
            context = ""

        return RetrievalResult(
            query=query,
            raw_text=raw,
            chunks=chunks,
            context=context,
            trace_id=self.client.last_trace_id or trace_id,
        )
