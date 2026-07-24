"""
大模型封装（llm.py）
====================

学习要点
--------
本应用**不持有** DASHSCOPE_API_KEY；一律经中台 MCP `chat_completion`。
型号：.env 的 LLM_MODEL 可选覆盖；空则用中台「大模型管理」项目配置。

对外方法：
  chat(messages)              — 通用（意图 / Agent 规划 / judge / fallback）
  generate(q, context, ...)   — 员工 RAG 模板（知识库片段 + 问题）
  generate_customer(...)      — 客户侧「工具结果 + 问题」模板

extract_json_object：
  规划/质检提示词要求「只输出 JSON」，但模型有时夹杂说明文字；
  本函数尽量抽出第一个 {...}，解析失败返回 None，由调用方走默认策略。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Sequence

from prompts import CUSTOMER_SYSTEM, EMPLOYEE_SYSTEM

logger = logging.getLogger(__name__)

# 兼容旧代码：DEFAULT_SYSTEM_PROMPT == 员工默认人设
DEFAULT_SYSTEM_PROMPT = EMPLOYEE_SYSTEM


class LlmError(RuntimeError):
    """MCP 调用失败或返回无法解析时抛出。"""


def _parse_chat_result(raw: str) -> str:
    """
    中台 chat_completion 可能返回：
      - JSON：{"content": "..."}
      - 纯文本 content
      - 以「大模型调用失败」开头的错误串
    """
    text = (raw or "").strip()
    if not text:
        raise LlmError("MCP chat_completion 返回为空")
    if text.startswith("大模型调用失败") or text.startswith("MCP"):
        raise LlmError(text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        if len(text) > 2:
            return text
        raise LlmError(f"无法解析大模型返回: {text[:200]}") from exc
    if isinstance(data, dict):
        content = data.get("content")
        if content:
            return str(content).strip()
        if data.get("error"):
            raise LlmError(str(data["error"]))
    raise LlmError(f"大模型返回中无 content: {text[:200]}")


def extract_json_object(text: str) -> dict[str, Any] | None:
    """从模型输出中尽量抽出第一个 JSON 对象。"""
    raw = (text or "").strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        try:
            data = json.loads(raw[start : end + 1])
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            return None
    return None


class McpChat:
    """通过中台 MCP `chat_completion` 生成回答。"""

    def __init__(
        self,
        *,
        project_id: str,
        model: str | None = None,
        client: Any = None,
    ) -> None:
        self.project_id = (project_id or "").strip()
        # 空 = 不传 model 参数，交给中台项目配置
        self.model = (model or "").strip() or None
        self._client = client

    @property
    def available(self) -> bool:
        """有 PROJECT_ID 才认为可以走大模型（密钥在中台侧）。"""
        return bool(self.project_id)

    def _mcp(self):
        if self._client is not None:
            return self._client
        from mcp_client import build_mcp_client

        return build_mcp_client()

    def chat(
        self,
        messages: Sequence[dict[str, str]],
        *,
        temperature: float | None = None,
    ) -> str:
        """通用对话；messages 已是 OpenAI 风格 [{role, content}, ...]。"""
        if not self.project_id:
            raise LlmError("未配置 PROJECT_ID，无法经中台调用大模型")

        params: dict[str, Any] = {
            "project_id": self.project_id,
            "messages": json.dumps(list(messages), ensure_ascii=False),
        }
        if self.model:
            params["model"] = self.model
        if temperature is not None:
            params["temperature"] = temperature

        try:
            raw = self._mcp().call_tool("chat_completion", params)
        except Exception as exc:
            raise LlmError(f"MCP 大模型调用失败: {exc}") from exc
        return _parse_chat_result(raw)

    def generate(
        self,
        question: str,
        context: str,
        *,
        history: Sequence[tuple[str, str]] | None = None,
        system_prompt: str | None = None,
    ) -> str:
        """员工路径：知识库片段 + 问题（历史最多带最近 6 轮）。"""
        kb = context.strip() if context.strip() else "（无召回片段）"
        user_content = (
            f"【知识库片段】\n{kb}\n\n"
            f"【用户问题】\n{question}\n\n"
            f"请基于知识库片段作答："
        )
        sys_text = (system_prompt or EMPLOYEE_SYSTEM).strip() or EMPLOYEE_SYSTEM
        messages: list[dict[str, str]] = [{"role": "system", "content": sys_text}]
        if history:
            for role, content in history[-6:]:
                if role in ("user", "assistant") and content:
                    messages.append({"role": role, "content": content})
        messages.append({"role": "user", "content": user_content})
        return self.chat(messages)

    def generate_customer(
        self,
        question: str,
        tool_context: str,
        *,
        system_prompt: str | None = None,
    ) -> str:
        """客户路径辅助：工具/补充上下文 + 问题（fallback 也可直接用 chat）。"""
        ctx = tool_context.strip() if tool_context.strip() else "（无工具结果）"
        user_content = (
            f"【已收集信息 / 工具结果】\n{ctx}\n\n"
            f"【用户问题】\n{question}\n\n"
            f"请基于以上信息作答："
        )
        sys_text = (system_prompt or CUSTOMER_SYSTEM).strip() or CUSTOMER_SYSTEM
        return self.chat(
            [
                {"role": "system", "content": sys_text},
                {"role": "user", "content": user_content},
            ]
        )
