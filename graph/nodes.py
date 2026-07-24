"""
客服图节点（graph/nodes.py）
============================

学习要点
--------
每个「节点」是一个 (state) -> partial_state 的函数；
「路由函数」返回下一个节点名字符串，供 add_conditional_edges 使用。

建议阅读顺序：
  1. route_intent / route_by_role     — 客户 vs 员工入口
  2. employee_answer                 — 员工短路径
  3. agent_plan → route_after_plan   — 客户 Agent
  4. clarify（含 interrupt）         — Clarify Loop
  5. run_tools → judge → route_after_judge — Tool Loop
  6. quote_lead（配置后询报价留资）
  7. fallback_answer / finalize

interrupt 是什么？
  LangGraph 在 clarify / quote_lead 里调用 interrupt(payload) 时图会暂停并把 payload
  暴露给 UI；用户补充后 service.resume_clarify 用 Command(resume=...)
  从断点继续，resume 的值变成 interrupt() 的返回值。

NodeContext：把 store/harness/llm/tools 注入闭包，避免全局单例满天飞。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from config import settings
from crm import (
    QUOTE_CONTACT_ASK,
    QUOTE_DECLINE_ACK,
    QUOTE_LEAD_GIVE_UP,
    QUOTE_LEAD_THANKS,
    QUOTE_OFFER_TAIL,
    build_lead_payload,
    config_proposal_text,
    extract_resume_text,
    lead_info_complete,
    missing_fields_ask,
    parse_customer_lead,
    register_crm_lead,
    wants_quote,
)
from harness import CustomerHarness
from llm import LlmError, McpChat, extract_json_object
from prompts import (
    AGENT_PLAN_SYSTEM,
    EMPLOYEE_SYSTEM,
    FALLBACK_SYSTEM,
    INTENT_SYSTEM,
    JUDGE_SYSTEM,
)
from store import ChatStore
from tools import AVAILABLE_TOOLS, CustomerTools

from graph.state import CustomerState

logger = logging.getLogger(__name__)

# 用户明显在要图/视频时，强制带上 search_media（避免 Agent 只选 rag_search）
_MEDIA_ASK_HINTS = (
    "图片",
    "照片",
    "相片",
    "视频",
    "影像",
    "看看",
    "发我",
    "发给我",
    "示意图",
    "外观图",
    "实拍",
    "有没有图",
)


def _wants_media(text: str) -> bool:
    q = text or ""
    return any(h in q for h in _MEDIA_ASK_HINTS)


@dataclass
class NodeContext:
    """节点共享依赖（由 build_graph 构造一次）。"""

    store: ChatStore
    harness: CustomerHarness
    llm: McpChat
    tools: CustomerTools
    employee_system: str = EMPLOYEE_SYSTEM


def _history_tuples(state: CustomerState) -> list[tuple[str, str]]:
    """把 state.history 转成 llm.generate 需要的 (role, content) 列表。"""
    out: list[tuple[str, str]] = []
    for item in state.get("history") or []:
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            out.append((str(item[0]), str(item[1])))
    return out


def _limit_attachments(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """同轮最多 1 个视频，或最多 3 张图（视频优先截断图）。"""
    videos = [a for a in items if a.get("type") == "video"]
    images = [a for a in items if a.get("type") != "video"]
    if videos:
        return videos[:1]
    return images[:3]


def _tool_context(state: CustomerState) -> str:
    """拼给 judge / fallback 的「已收集信息」文本。"""
    parts: list[str] = []
    extras = (state.get("extras") or "").strip()
    if extras:
        parts.append(f"【客户补充】\n{extras}")
    for i, tr in enumerate(state.get("tool_results") or [], 1):
        name = tr.get("tool") or "tool"
        text = tr.get("text") or ""
        parts.append(f"【工具 {i}: {name}】\n{text}")
    return "\n\n".join(parts) if parts else ""


def _has_process_config(state: CustomerState) -> bool:
    """本轮是否已产出工艺/设备配置方案（触发报价询问）。"""
    for tr in state.get("tool_results") or []:
        if tr.get("tool") == "process_config_recommend" and tr.get("ok"):
            return True
    return False


def make_nodes(ctx: NodeContext) -> dict[str, Any]:
    """工厂：返回节点名 → 可调用对象（含路由函数）。"""

    # ------------------------------------------------------------------
    # 意图分流
    # ------------------------------------------------------------------
    def route_intent(state: CustomerState) -> dict[str, Any]:
        """
        决定最终 role，并落库用户问题。

        auto_intent=False → 严格用侧栏 role_hint
        auto_intent=True  → LLM 分类，失败回退 hint
        """
        hint = (state.get("role_hint") or "customer").strip().lower()
        if hint not in ("customer", "employee"):
            hint = "customer"
        auto = bool(state.get("auto_intent"))
        role = hint
        sid = state.get("session_id")

        if auto and ctx.llm.available:
            try:
                raw = ctx.llm.chat(
                    [
                        {"role": "system", "content": INTENT_SYSTEM},
                        {
                            "role": "user",
                            "content": state.get("question") or "",
                        },
                    ]
                )
                data = extract_json_object(raw) or {}
                guessed = str(data.get("role") or "").strip().lower()
                if guessed in ("customer", "employee"):
                    role = guessed
                ctx.harness.emit(
                    "intent_classified",
                    session_id=sid,
                    role=role,
                    hint=hint,
                    reason=data.get("reason"),
                )
            except LlmError as exc:
                ctx.harness.emit(
                    "intent_fallback",
                    session_id=sid,
                    error=str(exc),
                    role=hint,
                )
                role = hint
        else:
            ctx.harness.emit(
                "intent_forced",
                session_id=sid,
                role=role,
                auto_intent=auto,
            )

        q = (state.get("question") or "").strip()
        if sid and q:
            ctx.store.add_message(sid, "user", q)

        return {
            "role": role,
            "status": "running",
            "tool_round": int(state.get("tool_round") or 0),
            "clarify_round": int(state.get("clarify_round") or 0),
            "tool_results": list(state.get("tool_results") or []),
            "sources": list(state.get("sources") or []),
            "extras": state.get("extras") or "",
            "degraded": False,
            "error": "",
        }

    def route_by_role(state: CustomerState) -> str:
        """条件边：员工短路径 / 客户 Agent 路径。"""
        return "employee" if state.get("role") == "employee" else "customer"

    # ------------------------------------------------------------------
    # 员工路径：RAG → 生成（无 Agent、无追问）
    # ------------------------------------------------------------------
    def employee_answer(state: CustomerState) -> dict[str, Any]:
        question = (state.get("question") or "").strip()
        sid = state.get("session_id")
        rag = ctx.tools.rag_search(question)
        sources = list(rag.get("sources") or [])
        context = rag.get("text") or ""
        trace_id = rag.get("trace_id")

        if not ctx.llm.available:
            answer = (
                "（未配置 PROJECT_ID，以下为知识库召回结果）\n\n" + context
                if context
                else "未配置 PROJECT_ID，且知识库无匹配结果。"
            )
            return {
                "answer": answer,
                "sources": sources,
                "trace_id": trace_id or "",
                "tool_results": [rag],
                "degraded": True,
                "status": "done",
                "error": "missing PROJECT_ID",
            }

        try:
            answer = ctx.llm.generate(
                question,
                context,
                history=_history_tuples(state),
                system_prompt=ctx.employee_system,
            )
            degraded = False
            err = ""
        except LlmError as exc:
            answer = (
                f"大模型生成失败：{exc}\n\n--- 知识库召回 ---\n{context or '（无）'}"
            )
            degraded = True
            err = str(exc)

        ctx.harness.emit("employee_answered", session_id=sid, degraded=degraded)
        return {
            "answer": answer,
            "sources": sources,
            "trace_id": trace_id or "",
            "tool_results": [rag],
            "degraded": degraded,
            "status": "done",
            "error": err,
        }

    # ------------------------------------------------------------------
    # 客户路径：Agent 规划
    # ------------------------------------------------------------------
    def agent_plan(state: CustomerState) -> dict[str, Any]:
        """
        产出 plan：选哪些工具、信息是否够、追问话术、tool_query。
        LLM 不可用或解析失败时退回「只 rag_search、信息够」。
        """
        question = (state.get("question") or "").strip()
        extras = (state.get("extras") or "").strip()
        sid = state.get("session_id")
        want_media = _wants_media(question) or _wants_media(extras)
        default_tools = ["search_media", "rag_search"] if want_media else ["rag_search"]
        default_plan = {
            "tools": default_tools,
            "info_enough": True,
            "missing_info": [],
            "clarify_question": "",
            "tool_query": question,
        }

        if not ctx.llm.available:
            ctx.harness.emit("agent_plan_default", session_id=sid, reason="no_llm")
            return {"plan": default_plan, "status": "running"}

        user_blob = f"用户问题：{question}"
        if extras:
            user_blob += f"\n已补充：{extras}"
        prior = state.get("tool_results") or []
        if prior:
            user_blob += f"\n已有工具轮次：{len(prior)}"
        if want_media:
            user_blob += "\n系统提示：用户在要图片/视频，tools 必须包含 search_media。"

        try:
            raw = ctx.llm.chat(
                [
                    {"role": "system", "content": AGENT_PLAN_SYSTEM},
                    {"role": "user", "content": user_blob},
                ]
            )
            data = extract_json_object(raw) or {}
        except LlmError as exc:
            logger.warning("agent_plan LLM 失败: %s", exc)
            ctx.harness.emit("agent_plan_error", session_id=sid, error=str(exc))
            return {"plan": default_plan, "status": "running"}

        tools = [
            t
            for t in (data.get("tools") or [])
            if isinstance(t, str) and t in AVAILABLE_TOOLS
        ]
        if not tools:
            tools = list(default_tools)
        if want_media and "search_media" not in tools:
            tools = ["search_media", *tools]
        plan = {
            "tools": tools,
            "info_enough": bool(data.get("info_enough", True)),
            "missing_info": list(data.get("missing_info") or []),
            "clarify_question": str(data.get("clarify_question") or "").strip(),
            "tool_query": str(data.get("tool_query") or question).strip() or question,
        }
        # 追问已到上限：强制进工具，避免无限 Clarify
        clarify_round = int(state.get("clarify_round") or 0)
        if clarify_round >= settings.max_clarify_loops:
            plan["info_enough"] = True
        ctx.harness.emit(
            "agent_plan",
            session_id=sid,
            tools=tools,
            info_enough=plan["info_enough"],
        )
        return {"plan": plan, "status": "running"}

    def route_after_plan(state: CustomerState) -> str:
        plan = state.get("plan") or {}
        if not plan.get("info_enough", True):
            return "clarify"
        return "run_tools"

    # ------------------------------------------------------------------
    # Clarify Loop：interrupt 问用户
    # ------------------------------------------------------------------
    def clarify(state: CustomerState) -> dict[str, Any]:
        from langgraph.types import interrupt

        round_n = int(state.get("clarify_round") or 0) + 1
        sid = state.get("session_id")
        ok, msg = ctx.harness.guard_clarify_round(round_n)
        if not ok:
            # 超限：不 interrupt，改 plan 让 route_after_clarify 去 run_tools
            ctx.harness.emit("clarify_limit", session_id=sid, round=round_n)
            plan = dict(state.get("plan") or {})
            plan["info_enough"] = True
            return {
                "clarify_round": round_n,
                "clarify_question": msg,
                "plan": plan,
                "status": "running",
            }

        plan = state.get("plan") or {}
        question = (
            plan.get("clarify_question")
            or "为更好协助您，请补充：物料、目标细度、产量、进料尺寸等关键信息。"
        )
        if sid:
            ctx.store.add_message(sid, "assistant", question)
        ctx.harness.emit("clarify_ask", session_id=sid, round=round_n)

        # 图在此暂停；UI 看到 interrupted=True 与 payload.question
        resume = interrupt(
            {
                "type": "clarify",
                "session_id": sid,
                "question": question,
                "missing_info": plan.get("missing_info") or [],
                "round": round_n,
                "hint": "请补充信息；resume: {user_reply: ...}",
            }
        )
        # ---- 以下仅在 resume_clarify 之后继续执行 ----
        reply = ""
        if isinstance(resume, str):
            reply = resume
        elif isinstance(resume, dict):
            reply = str(resume.get("user_reply") or resume.get("text") or "")
        reply = reply.strip()
        extras = (state.get("extras") or "").strip()
        if reply:
            extras = (extras + "\n" + reply).strip() if extras else reply
            if sid:
                ctx.store.add_message(sid, "user", reply)

        out: dict[str, Any] = {
            "clarify_round": round_n,
            "clarify_question": question,
            "user_reply": reply,
            "extras": extras,
            "status": "need_clarify",
        }
        # 来自工艺助手缺槽：补全后直接再调工具，避免再绕 agent
        if state.get("need_slot_clarify"):
            plan = dict(state.get("plan") or {})
            plan["info_enough"] = True
            if "process_config_recommend" not in (plan.get("tools") or []):
                plan["tools"] = ["process_config_recommend"]
            out["plan"] = plan
            out["need_slot_clarify"] = False
            out["status"] = "running"
        return out

    def route_after_clarify(state: CustomerState) -> str:
        plan = state.get("plan") or {}
        if plan.get("info_enough"):
            return "run_tools"
        return "agent_plan"

    # ------------------------------------------------------------------
    # Tool Loop：执行工具 → 缺槽 Clarify / 质检
    # ------------------------------------------------------------------
    def run_tools(state: CustomerState) -> dict[str, Any]:
        round_n = int(state.get("tool_round") or 0) + 1
        sid = state.get("session_id")
        ok, msg = ctx.harness.guard_tool_round(round_n)
        if not ok:
            ctx.harness.emit("tool_limit", session_id=sid, round=round_n)
            return {
                "tool_round": round_n,
                "answer_ok": False,
                "error": msg,
                "need_slot_clarify": False,
                "status": "running",
            }

        plan = state.get("plan") or {}
        query = str(plan.get("tool_query") or state.get("question") or "").strip()
        extras = state.get("extras") or ""
        process_slots = dict(state.get("process_slots") or {})
        tools = [
            t
            for t in (plan.get("tools") or ["rag_search"])
            if t in AVAILABLE_TOOLS
        ] or ["rag_search"]

        results = list(state.get("tool_results") or [])
        sources = list(state.get("sources") or [])
        attachments = list(state.get("attachments") or [])
        trace_id = state.get("trace_id") or ""
        need_slot_clarify = False
        clarify_plan = dict(plan)

        for name in tools:
            tr = ctx.tools.run(
                name,
                query,
                extras=extras,
                slots=process_slots if name == "process_config_recommend" else None,
                session_id=sid or "",
            )
            results.append(tr)
            for s in tr.get("sources") or []:
                sources.append(s)
            for a in tr.get("attachments") or []:
                if isinstance(a, dict) and a.get("asset_id"):
                    # 同轮去重
                    if not any(
                        x.get("asset_id") == a.get("asset_id") for x in attachments
                    ):
                        attachments.append(a)
            if tr.get("trace_id"):
                trace_id = str(tr["trace_id"])
            if isinstance(tr.get("slots_partial"), dict) and tr["slots_partial"]:
                process_slots = {**process_slots, **tr["slots_partial"]}

            if (
                name == "process_config_recommend"
                and tr.get("error") == "missing_required_slots"
            ):
                need_slot_clarify = True
                labels = tr.get("missing_labels") or tr.get("missing") or []
                ask = str(
                    tr.get("clarify_question")
                    or tr.get("text")
                    or (
                        "生成配置清单前还需确认："
                        + "、".join(str(x) for x in labels)
                        + "。"
                    )
                ).strip()
                clarify_plan = {
                    **clarify_plan,
                    "tools": ["process_config_recommend"],
                    "info_enough": False,
                    "missing_info": list(labels),
                    "clarify_question": ask,
                    "tool_query": query,
                }
                ctx.harness.emit(
                    "process_slots_missing",
                    session_id=sid,
                    missing=tr.get("missing"),
                )

        # 同轮附件上限：最多 1 视频或 3 图
        attachments = _limit_attachments(attachments)

        ctx.harness.emit(
            "tools_ran",
            session_id=sid,
            round=round_n,
            tools=tools,
            need_slot_clarify=need_slot_clarify,
        )
        out: dict[str, Any] = {
            "tool_round": round_n,
            "tool_results": results,
            "sources": sources,
            "attachments": attachments,
            "trace_id": trace_id,
            "process_slots": process_slots,
            "need_slot_clarify": need_slot_clarify,
            "status": "running",
            "error": "",
        }
        if need_slot_clarify:
            out["plan"] = clarify_plan
        return out

    def route_after_tools(state: CustomerState) -> str:
        if state.get("need_slot_clarify"):
            return "clarify"
        return "judge"

    def judge(state: CustomerState) -> dict[str, Any]:
        """判断工具结果是否足够；够则写出 draft_answer。"""
        sid = state.get("session_id")
        if (state.get("error") or "").startswith("已超过最大工具"):
            return {"answer_ok": False, "status": "running"}

        question = state.get("question") or ""
        ctx_text = _tool_context(state)
        if not ctx.llm.available:
            answer = ctx_text or "暂无可用工具结果，且未配置大模型。"
            return {
                "answer_ok": True,
                "answer": answer,
                "degraded": True,
                "status": "done",
                "error": "missing PROJECT_ID",
            }

        try:
            raw = ctx.llm.chat(
                [
                    {"role": "system", "content": JUDGE_SYSTEM},
                    {
                        "role": "user",
                        "content": (
                            f"用户问题：{question}\n\n工具与补充：\n{ctx_text or '（无）'}"
                        ),
                    },
                ]
            )
            data = extract_json_object(raw) or {}
        except LlmError as exc:
            ctx.harness.emit("judge_error", session_id=sid, error=str(exc))
            return {
                "answer_ok": False,
                "degraded": True,
                "error": str(exc),
                "status": "running",
            }

        answer_ok = bool(data.get("answer_ok"))
        draft = str(data.get("draft_answer") or "").strip()
        out: dict[str, Any] = {
            "answer_ok": answer_ok,
            "status": "running",
        }
        if answer_ok and draft:
            out["answer"] = draft
            out["status"] = "done"
        ctx.harness.emit(
            "judge",
            session_id=sid,
            answer_ok=answer_ok,
            reason=data.get("reason"),
        )
        return out

    def route_after_judge(state: CustomerState) -> str:
        if state.get("status") == "done" or state.get("answer_ok"):
            if _has_process_config(state) and not state.get("quote_flow_done"):
                return "quote_offer"
            return "finalize"
        tool_round = int(state.get("tool_round") or 0)
        if tool_round >= settings.max_tool_loops:
            return "fallback"
        # 未超限且不满意 → 再规划（可能换工具或再 Clarify）
        return "agent_plan"

    def fallback_answer(state: CustomerState) -> dict[str, Any]:
        """工具用尽：带着全部已收集信息尽力作答（对应图中「直出 LLM」）。"""
        sid = state.get("session_id")
        question = state.get("question") or ""
        ctx_text = _tool_context(state)
        ctx.harness.emit("fallback", session_id=sid, tool_round=state.get("tool_round"))

        if not ctx.llm.available:
            return {
                "answer": ctx_text or "工具调用已达上限，且未配置大模型，无法生成答复。",
                "degraded": True,
                "status": "done",
                "error": state.get("error") or "fallback_no_llm",
            }

        try:
            answer = ctx.llm.chat(
                [
                    {"role": "system", "content": FALLBACK_SYSTEM},
                    {
                        "role": "user",
                        "content": (
                            f"【已收集信息】\n{ctx_text or '（无）'}\n\n"
                            f"【用户问题】\n{question}"
                        ),
                    },
                ]
            )
            return {"answer": answer, "degraded": True, "status": "done", "error": ""}
        except LlmError as exc:
            return {
                "answer": f"兜底生成失败：{exc}\n\n{ctx_text or ''}",
                "degraded": True,
                "status": "done",
                "error": str(exc),
            }

    def route_after_fallback(state: CustomerState) -> str:
        if _has_process_config(state) and not state.get("quote_flow_done"):
            return "quote_offer"
        return "finalize"

    def _save_quote_lead(
        state: CustomerState,
        *,
        proposal: str,
        draft: str,
        name: str,
        contact: str,
        raw_replies: list[str],
    ) -> dict[str, Any]:
        """姓名+联系方式齐全后落库并生成致谢文案。"""
        sid = state.get("session_id")
        payload = build_lead_payload(
            {
                **dict(state),
                "config_proposal": proposal,
                "answer": draft,
            },
            customer_name=name,
            customer_contact=contact,
            raw_replies=raw_replies,
        )
        lead_id = ""
        try:
            lead_id = register_crm_lead(ctx.store, payload, session_id=sid)
            ctx.harness.emit(
                "crm_lead_saved",
                session_id=sid,
                lead_id=lead_id,
                name=name,
                contact=contact,
            )
        except Exception as exc:
            logger.warning("crm lead 落库失败: %s", exc)
            ctx.harness.emit("crm_lead_error", session_id=sid, error=str(exc))

        thanks = (
            f"{QUOTE_LEAD_THANKS}\n\n"
            f"已登记：{name} / {contact}"
        )
        if lead_id:
            thanks += f"\n登记编号：`{lead_id}`"
        return {
            "config_proposal": proposal,
            "quote_flow_done": True,
            "lead_phase": "done",
            "lead_id": lead_id,
            "lead_replies": list(raw_replies),
            "answer": thanks,
            "status": "done",
            "degraded": False,
        }

    def quote_offer(state: CustomerState) -> dict[str, Any]:
        """
        推送配置方案并询问是否报价（单次 interrupt）。

        客户回「需要」后进入 quote_collect 追问姓名+联系方式；
        若首轮已带齐姓名与联系方式则直接落库。
        """
        from langgraph.types import interrupt

        sid = state.get("session_id")
        draft = (state.get("answer") or "").strip()
        proposal = config_proposal_text({**state, "answer": draft})

        offer_text = (draft + QUOTE_OFFER_TAIL) if draft else (
            "已为您生成设备配置方案。" + QUOTE_OFFER_TAIL
        )
        if sid:
            ctx.store.add_message(sid, "assistant", offer_text)
        ctx.harness.emit("quote_offer", session_id=sid)

        resume = interrupt(
            {
                "type": "quote_offer",
                "session_id": sid,
                "question": offer_text,
                "hint": "回复「需要」后将继续收集姓名与联系方式",
            }
        )
        reply = extract_resume_text(resume)
        replies: list[str] = []
        if reply:
            replies.append(reply)
            if sid:
                ctx.store.add_message(sid, "user", reply)

        if wants_quote(reply) is False:
            ctx.harness.emit("quote_declined", session_id=sid)
            return {
                "config_proposal": proposal,
                "quote_flow_done": True,
                "lead_phase": "done",
                "lead_replies": replies,
                "answer": QUOTE_DECLINE_ACK,
                "status": "done",
                "degraded": False,
            }

        name, contact, missing = parse_customer_lead("\n".join(replies))
        if lead_info_complete(name, contact):
            return _save_quote_lead(
                state,
                proposal=proposal,
                draft=draft,
                name=name,
                contact=contact,
                raw_replies=replies,
            )

        # 仅回「需要」或信息不齐 → 进入联系方式收集节点
        ask = (
            QUOTE_CONTACT_ASK
            if not name and not contact
            else missing_fields_ask(
                missing, have_name=name, have_contact=contact
            )
        )
        ctx.harness.emit(
            "quote_need_contact",
            session_id=sid,
            missing=missing,
            reply=reply,
        )
        return {
            "config_proposal": proposal,
            "quote_flow_done": False,
            "lead_phase": "collect",
            "lead_replies": replies,
            "lead_ask": ask,
            "lead_round": 0,
            "answer": ask,
            "status": "need_clarify",
            "degraded": False,
        }

    def route_after_quote_offer(state: CustomerState) -> str:
        if state.get("quote_flow_done") or state.get("lead_phase") == "done":
            return "finalize"
        if state.get("lead_phase") == "collect":
            return "quote_collect"
        return "finalize"

    def quote_collect(state: CustomerState) -> dict[str, Any]:
        """
        单次 interrupt 收集姓名+联系方式；不齐则路由回本节点继续问。
        """
        from langgraph.types import interrupt

        sid = state.get("session_id")
        draft = (state.get("answer") or "").strip()
        # answer 可能已被写成追问文案，配置稿优先用 config_proposal
        proposal = (state.get("config_proposal") or "").strip() or config_proposal_text(
            state
        )
        replies = list(state.get("lead_replies") or [])
        ask = (state.get("lead_ask") or QUOTE_CONTACT_ASK).strip() or QUOTE_CONTACT_ASK
        round_n = int(state.get("lead_round") or 0) + 1
        max_rounds = max(1, settings.max_clarify_loops)

        if round_n > max_rounds:
            ctx.harness.emit("quote_contact_limit", session_id=sid, round=round_n)
            return {
                "quote_flow_done": True,
                "lead_phase": "done",
                "answer": QUOTE_LEAD_GIVE_UP,
                "status": "done",
                "degraded": True,
            }

        if sid:
            ctx.store.add_message(sid, "assistant", ask)
        name0, contact0, missing0 = parse_customer_lead("\n".join(replies))
        ctx.harness.emit(
            "quote_ask_contact",
            session_id=sid,
            round=round_n,
            missing=missing0,
            have_name=name0,
            have_contact=contact0,
        )

        resume = interrupt(
            {
                "type": "quote_contact",
                "session_id": sid,
                "question": ask,
                "missing": list(missing0),
                "have_name": name0,
                "have_contact": contact0,
                "round": round_n,
                "hint": "请提供姓名与手机号/邮箱；取消请回「不需要」",
            }
        )
        reply = extract_resume_text(resume)
        if reply:
            replies.append(reply)
            if sid:
                ctx.store.add_message(sid, "user", reply)

        if wants_quote(reply) is False:
            ctx.harness.emit("quote_declined", session_id=sid)
            return {
                "quote_flow_done": True,
                "lead_phase": "done",
                "lead_replies": replies,
                "lead_round": round_n,
                "answer": QUOTE_DECLINE_ACK,
                "status": "done",
                "degraded": False,
            }

        name, contact, missing = parse_customer_lead("\n".join(replies))
        if lead_info_complete(name, contact):
            return _save_quote_lead(
                state,
                proposal=proposal,
                draft=proposal or draft,
                name=name,
                contact=contact,
                raw_replies=replies,
            )

        next_ask = missing_fields_ask(
            missing, have_name=name, have_contact=contact
        )
        return {
            "quote_flow_done": False,
            "lead_phase": "collect",
            "lead_replies": replies,
            "lead_ask": next_ask,
            "lead_round": round_n,
            "answer": next_ask,
            "status": "need_clarify",
            "degraded": False,
        }

    def route_after_quote_collect(state: CustomerState) -> str:
        if state.get("quote_flow_done") or state.get("lead_phase") == "done":
            return "finalize"
        if state.get("lead_phase") == "collect":
            return "quote_collect"
        return "finalize"

    def finalize(state: CustomerState) -> dict[str, Any]:
        """统一出口：落库助手消息（员工/客户/fallback/报价确认 都经过这里）。"""
        sid = state.get("session_id")
        answer = (state.get("answer") or "").strip()
        if not answer:
            answer = "暂时无法给出答复，请稍后重试或联系人工客服。"

        if sid:
            sources = state.get("sources") or []
            attachments = state.get("attachments") or []
            payload_sources = list(sources)
            if attachments:
                payload_sources.append({"kind": "attachments", "items": attachments})
            ctx.store.add_message(
                sid,
                "assistant",
                answer,
                sources=json.dumps(payload_sources, ensure_ascii=False)
                if payload_sources
                else None,
                trace_id=state.get("trace_id") or None,
            )
        ctx.harness.emit(
            "finalize",
            session_id=sid,
            role=state.get("role"),
            tool_round=state.get("tool_round"),
            clarify_round=state.get("clarify_round"),
            lead_id=state.get("lead_id") or "",
            attachment_count=len(state.get("attachments") or []),
        )
        return {
            "answer": answer,
            "status": "done",
            "attachments": list(state.get("attachments") or []),
        }

    return {
        "route_intent": route_intent,
        "route_by_role": route_by_role,
        "employee_answer": employee_answer,
        "agent_plan": agent_plan,
        "route_after_plan": route_after_plan,
        "clarify": clarify,
        "route_after_clarify": route_after_clarify,
        "run_tools": run_tools,
        "route_after_tools": route_after_tools,
        "judge": judge,
        "route_after_judge": route_after_judge,
        "fallback_answer": fallback_answer,
        "route_after_fallback": route_after_fallback,
        "quote_offer": quote_offer,
        "route_after_quote_offer": route_after_quote_offer,
        "quote_collect": quote_collect,
        "route_after_quote_collect": route_after_quote_collect,
        "finalize": finalize,
    }
