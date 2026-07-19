"""
AI 客服 — 官方 MCP Streamable HTTP 客户端
==========================================

连接数据中台 MCP（默认 http://127.0.0.1:8765/mcp），
使用官方 MCP Python SDK 的 Streamable HTTP 传输。

兼容：若 MCP_TRANSPORT=tcp，则回退到遗留 TCP（过渡期）。
"""

from __future__ import annotations

import asyncio
import json
import socket
from typing import Any


class MCPClientError(Exception):
    """MCP 调用失败（网络、协议或服务端 error）。"""


def _extract_tool_text(result: Any) -> str:
    """从官方 call_tool 结果中取出文本。"""
    if result is None:
        return ""
    # CallToolResult: content list of TextContent
    content = getattr(result, "content", None)
    if content is None and isinstance(result, dict):
        content = result.get("content")
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            text = getattr(item, "text", None)
            if text is None and isinstance(item, dict):
                text = item.get("text")
            if text:
                parts.append(str(text))
        if parts:
            return "\n".join(parts)
    structured = getattr(result, "structuredContent", None) or getattr(result, "data", None)
    if structured is not None:
        return structured if isinstance(structured, str) else json.dumps(structured, ensure_ascii=False)
    return str(result)


class MCPHttpClient:
    """官方 Streamable HTTP MCP 客户端（同步封装）。"""

    def __init__(
        self,
        url: str,
        *,
        bearer_token: str = "",
        timeout: float = 120,
    ) -> None:
        self.url = url.rstrip("/")
        self.bearer_token = bearer_token
        self.timeout = timeout
        self.last_trace_id: str | None = None

    def _headers(self, trace_id: str | None = None) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self.bearer_token:
            headers["Authorization"] = f"Bearer {self.bearer_token}"
        if trace_id:
            headers["X-Trace-Id"] = trace_id
            self.last_trace_id = trace_id
        return headers

    async def _call_tool_async(self, name: str, parameters: dict[str, Any], trace_id: str | None) -> str:
        try:
            from mcp import ClientSession
            from mcp.client.streamable_http import streamablehttp_client
        except ImportError as exc:
            raise MCPClientError("请安装官方 mcp 包：pip install mcp") from exc

        headers = self._headers(trace_id)
        try:
            async with streamablehttp_client(
                self.url,
                headers=headers or None,
                timeout=self.timeout,
            ) as (read, write, _get_session_id):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool(name, parameters or {})
                    return _extract_tool_text(result)
        except MCPClientError:
            raise
        except Exception as exc:
            raise MCPClientError(f"MCP HTTP 调用失败 ({self.url}): {exc}") from exc

    async def _list_tools_async(self) -> list[dict[str, Any]]:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        async with streamablehttp_client(
            self.url,
            headers=self._headers() or None,
            timeout=self.timeout,
        ) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                out = []
                for t in tools.tools:
                    out.append(
                        {
                            "name": t.name,
                            "description": t.description or "",
                            "parameters": {},
                        }
                    )
                return out

    def list_tools(self) -> list[dict[str, Any]]:
        try:
            return asyncio.run(self._list_tools_async())
        except MCPClientError:
            raise
        except Exception as exc:
            raise MCPClientError(f"MCP list_tools 失败: {exc}") from exc

    def call_tool(
        self,
        name: str,
        parameters: dict[str, Any] | None = None,
        *,
        trace_id: str | None = None,
    ) -> str:
        try:
            return asyncio.run(self._call_tool_async(name, parameters or {}, trace_id))
        except MCPClientError:
            raise
        except Exception as exc:
            raise MCPClientError(f"MCP call_tool 失败: {exc}") from exc

    # 兼容旧接口名
    def call(self, method: str, params: dict | None = None, *, trace_id: str | None = None) -> Any:
        if method == "tools/list":
            return self.list_tools()
        if method == "tools/call":
            p = params or {}
            return self.call_tool(p.get("name"), p.get("parameters") or {}, trace_id=trace_id)
        raise MCPClientError(f"不支持的 method: {method}")


class MCPTcpClient:
    """遗留 TCP 客户端（过渡期 MCP_TRANSPORT=tcp 时使用）。"""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8766,
        auth_token: str = "",
        timeout: float = 120,
    ) -> None:
        self.host = host
        self.port = port
        self.auth_token = auth_token
        self.timeout = timeout
        self.last_trace_id: str | None = None

    def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        trace_id: str | None = None,
    ) -> Any:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method, "id": 1}
        if params is not None:
            payload["params"] = params
        if self.auth_token:
            payload["auth_token"] = self.auth_token
        if trace_id:
            payload["trace_id"] = trace_id

        raw = self._send_line(payload)
        self.last_trace_id = raw.get("trace_id")
        if raw.get("error"):
            err = raw["error"]
            message = err.get("message", err) if isinstance(err, dict) else str(err)
            tid = f" (trace_id={self.last_trace_id})" if self.last_trace_id else ""
            raise MCPClientError(
                f"MCP 错误 [{err.get('code') if isinstance(err, dict) else '?'}]: {message}{tid}"
            )
        return raw.get("result")

    def _send_line(self, payload: dict[str, Any]) -> dict[str, Any]:
        message = json.dumps(payload, ensure_ascii=False) + "\n"
        data = b""
        try:
            with socket.create_connection((self.host, self.port), timeout=self.timeout) as sock:
                sock.sendall(message.encode("utf-8"))
                while b"\n" not in data:
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    data += chunk
        except OSError as exc:
            raise MCPClientError(
                f"无法连接 MCP TCP {self.host}:{self.port}，请确认 MCP_ENABLE_TCP=true: {exc}"
            ) from exc

        if not data:
            raise MCPClientError("MCP 未返回数据")
        line = data.decode("utf-8").strip().split("\n")[0]
        try:
            return json.loads(line)
        except json.JSONDecodeError as exc:
            raise MCPClientError(f"MCP 响应非 JSON: {line[:200]}") from exc

    def list_tools(self) -> list[dict[str, Any]]:
        result = self.call("tools/list")
        return result if isinstance(result, list) else []

    def call_tool(
        self,
        name: str,
        parameters: dict[str, Any] | None = None,
        *,
        trace_id: str | None = None,
    ) -> str:
        result = self.call(
            "tools/call",
            {"name": name, "parameters": parameters or {}},
            trace_id=trace_id,
        )
        return str(result) if result is not None else ""


def build_mcp_client():
    """按配置创建 HTTP 或 TCP 客户端。"""
    from config import settings

    transport = (settings.mcp_transport or "http").lower()
    if transport == "tcp":
        return MCPTcpClient(
            host=settings.mcp_host,
            port=settings.mcp_port,
            auth_token=settings.mcp_tcp_secret,
            timeout=settings.mcp_timeout,
        )
    return MCPHttpClient(
        url=settings.mcp_url,
        bearer_token=settings.mcp_client_token or settings.mcp_tcp_secret,
        timeout=settings.mcp_timeout,
    )
