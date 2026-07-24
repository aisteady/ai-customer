"""
LangGraph 状态（graph/state.py）
================================

学习要点
--------
CustomerState 是整张客服图在节点之间传递的「黑板」。
每个节点 return 的 dict 会 merge 进 state；条件边只读 state 做路由。

字段分组记忆：
  会话：session_id / thread_id（thread_id = LangGraph checkpoint 键，interrupt 靠它恢复）
  输入：question / user_reply / extras（extras = Clarify 累积补充）
  分流：role_hint（侧栏）/ auto_intent / role（最终）
  Agent：plan（tools、info_enough、clarify_question、tool_query …）
  计数：tool_round / clarify_round（Harness 硬顶看这两个）
  产物：tool_results / sources / answer / answer_ok
  报价线索：config_proposal / quote_flow_done / lead_id
  展示：status / degraded / error / history

total=False：字段都可选，方便 interrupt 恢复时只带部分键。
更完整的流程说明见仓库内 FLOW_ANALYSIS.md。
"""

from __future__ import annotations

from typing import Any, TypedDict


class CustomerState(TypedDict, total=False):
    # ---- 会话与 checkpoint ----
    session_id: str  # 业务会话（写 messages 用）
    thread_id: str  # LangGraph thread_id（与 checkpoint 对齐）

    # ---- 本轮输入 ----
    question: str  # 用户原始问题（ask 时写入，后续节点只读）
    user_reply: str  # Clarify resume 时的本轮补充
    extras: str  # 多轮追问累积的补充文本

    # ---- 意图分流 ----
    role_hint: str  # 侧栏：customer | employee
    auto_intent: bool  # True 时 LLM 可覆盖 role_hint
    role: str  # route_intent 之后的最终角色

    # ---- Agent 规划结果 ----
    plan: dict[str, Any]

    # ---- 双 Loop 计数 ----
    tool_round: int
    clarify_round: int
    clarify_question: str

    # ---- 工具与引用 ----
    tool_results: list[dict[str, Any]]
    sources: list[dict[str, Any]]
    attachments: list[dict[str, Any]]  # 图片/视频附件（经 search_media）
    trace_id: str

    # ---- 答复 ----
    answer: str
    answer_ok: bool
    status: str  # running | need_clarify | done | failed
    degraded: bool  # 降级 / 兜底时为 True
    error: str

    # ---- 配置方案后报价留资 ----
    config_proposal: str  # 推送给客户的配置方案正文
    quote_flow_done: bool  # 已完成报价询问（含婉拒或已留资）
    lead_id: str  # crm_leads 记录 id
    lead_phase: str  # "" | collect | done
    lead_replies: list[str]  # 客户留资相关回复累积
    lead_ask: str  # 当前向客户追问的文案
    lead_round: int  # 联系方式追问轮次

    # ---- 工艺五要素（工艺助手回传，跨轮合并）----
    process_slots: dict[str, Any]
    need_slot_clarify: bool  # 工具返回缺五要素 → 走 Clarify

    # 多轮历史：[[role, content], ...]；员工路径 generate 会用到
    history: list[list[str]]
