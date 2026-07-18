"""
AI 客服 Demo — MCP TCP 客户端
==============================

连接数据中台已启动的 MCP 服务（默认 tcp://127.0.0.1:8765），
发送 JSON-RPC 风格请求并解析响应。

协议要点（与 src/mcp/server.py 一致）：
- 每条消息为一行 JSON + 换行 \\n
- method: tools/list | tools/call
- 若服务端配置了 MCP_TCP_SECRET，请求根字段须含 auth_token

学习要点：
- 本 Demo 模拟「外部项目」通过 TCP 调用 MCP，与 Streamlit / FastAPI 主工程解耦
- 生产环境也可改用 HTTP 直连 FastAPI + PROJECT_SERVICE_TOKEN（见 .env.example 注释）
"""

from __future__ import annotations

import json
import socket
from typing import Any


class MCPClientError(Exception):
    """MCP 调用失败（网络、协议或服务端 error 字段）。"""


class MCPTcpClient:
    """
    同步 TCP MCP 客户端（Demo 够用；高并发场景可改为 asyncio）。

    Args:
        host: MCP 服务地址，默认 127.0.0.1
        port: MCP 端口，默认 8765
        auth_token: 与数据中台 .env 的 MCP_TCP_SECRET 一致；未配置 secret 时可留空
        timeout:  socket 超时秒数
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8765,
        auth_token: str = "",
        timeout: float = 120.0,
    ) -> None:
        self.host = host
        self.port = port
        self.auth_token = auth_token
        self.timeout = timeout
        self._request_id = 0
        self.last_trace_id: str | None = None

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        trace_id: str | None = None,
    ) -> Any:
        """
        发送单次 MCP 请求并返回 result 字段。

        可选传入 trace_id（根字段）；响应根字段也会带回同一 trace_id。
        最近一次响应的 trace_id 保存在 self.last_trace_id。

        Raises:
            MCPClientError: 连接失败或响应含 error
        """
        payload: dict[str, Any] = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params or {},
            "id": self._next_id(),
        }
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
        """写入一行 JSON，读取一行 JSON 响应。"""
        message = json.dumps(payload, ensure_ascii=False) + "\n"
        data = b""

        try:
            with socket.create_connection((self.host, self.port), timeout=self.timeout) as sock:
                sock.sendall(message.encode("utf-8"))
                # MCP Server 每条响应以换行结束
                while b"\n" not in data:
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    data += chunk
        except OSError as exc:
            raise MCPClientError(
                f"无法连接 MCP {self.host}:{self.port}，请确认已运行 start_mcp.py 且 API 已启动: {exc}"
            ) from exc

        if not data:
            raise MCPClientError("MCP 未返回数据")

        line = data.decode("utf-8").strip().split("\n")[0]
        try:
            return json.loads(line)
        except json.JSONDecodeError as exc:
            raise MCPClientError(f"MCP 响应非 JSON: {line[:200]}") from exc

    def list_tools(self) -> list[dict[str, Any]]:
        """tools/list → 工具定义列表。"""
        result = self.call("tools/list")
        return result if isinstance(result, list) else []

    def call_tool(
        self,
        name: str,
        parameters: dict[str, Any] | None = None,
        *,
        trace_id: str | None = None,
    ) -> str:
        """
        tools/call → 执行指定工具，返回文本结果。

        Example:
            client.call_tool("search_documents", {"query": "退款流程", "project_id": "..."})
        """
        result = self.call(
            "tools/call",
            {"name": name, "parameters": parameters or {}},
            trace_id=trace_id,
        )
        return str(result) if result is not None else ""
