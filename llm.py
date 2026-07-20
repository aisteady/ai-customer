"""
AI 客服 — DashScope LLM（RAG 的「G」）
======================================

只负责组 messages 并调用通义千问。
system_prompt 由 service.resolve_system_prompt 注入（可来自中台项目配置）。
用户消息里会附带【知识库片段】，约束模型依据片段作答。
"""

from __future__ import annotations

import logging
from typing import Sequence

logger = logging.getLogger(__name__)

DEFAULT_SYSTEM_PROMPT = """你是企业「AI 客服」助手。请严格根据提供的【知识库片段】回答用户问题。

规则：
1. 只依据知识库片段作答，不要编造政策、数字或流程。
2. 若片段不足以回答，请明确说明「根据现有知识库无法确定」，并建议用户联系人工客服。
3. 回答简洁、礼貌、分点说明；必要时引用文件名。
4. 不要输出与问题无关的内容。
"""

# 兼容旧引用名
SYSTEM_PROMPT = DEFAULT_SYSTEM_PROMPT


class LlmError(RuntimeError):
    pass


class DashScopeChat:
    def __init__(self, api_key: str, model: str = "qwen-plus") -> None:
        self.api_key = api_key
        self.model = model

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def generate(
        self,
        question: str,
        context: str,
        *,
        history: Sequence[tuple[str, str]] | None = None,
        system_prompt: str | None = None,
    ) -> str:
        if not self.api_key:
            raise LlmError("未配置 DASHSCOPE_API_KEY，无法生成回答")

        try:
            import dashscope
            from dashscope import Generation
            from http import HTTPStatus
        except ImportError as exc:
            raise LlmError("请安装 dashscope：uv add dashscope") from exc

        dashscope.api_key = self.api_key
        kb = context.strip() if context.strip() else "（无召回片段）"
        user_content = (
            f"【知识库片段】\n{kb}\n\n"
            f"【用户问题】\n{question}\n\n"
            f"请基于知识库片段作答："
        )

        sys_text = (system_prompt or DEFAULT_SYSTEM_PROMPT).strip() or DEFAULT_SYSTEM_PROMPT
        messages: list[dict] = [{"role": "system", "content": sys_text}]
        if history:
            for role, content in history[-6:]:
                if role in ("user", "assistant") and content:
                    messages.append({"role": role, "content": content})
        messages.append({"role": "user", "content": user_content})

        response = Generation.call(
            model=self.model,
            messages=messages,
            result_format="message",
        )
        status = getattr(response, "status_code", None)
        if status is not None and status != HTTPStatus.OK:
            raise LlmError(
                f"LLM 调用失败: status={status} message={getattr(response, 'message', '')}"
            )

        output = getattr(response, "output", None)
        if output is None:
            raise LlmError("LLM 返回为空")

        if isinstance(output, dict):
            choices = output.get("choices") or []
            if choices:
                msg = choices[0].get("message") or {}
                text = msg.get("content")
                if text:
                    return str(text).strip()
            text = output.get("text")
            if text:
                return str(text).strip()

        text = getattr(output, "text", None)
        if text:
            return str(text).strip()
        raise LlmError("LLM 返回中无可用文本")
