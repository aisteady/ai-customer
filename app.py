"""
Streamlit UI（app.py）
=====================

学习要点
--------
本文件**只做展示与会话态**，不写业务分支。
真正问答：CustomerService.ask / resume_clarify。

关键 session_state：
  session_id / thread_id  — 业务会话 & LangGraph checkpoint
  role_hint / auto_intent — 传给 ask 的分流参数
  pending_clarify         — True 时下一句走 resume_clarify（Clarify Loop）
  messages                — 当前页聊天气泡

Clarify 交互：
  ask 返回 interrupted=True → 展示追问，pending_clarify=True
  用户再输入 → resume_clarify(thread_id, 补充) → 可能再次 interrupt 或出最终答

注意：补充后 resume 会继续跑 Agent/工具/LLM，可能较慢；
完成后用 st.rerun() 整页刷新，避免「发出去没反应」的观感。

运行：
  cd models/ai_customer && uv run streamlit run app.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# 允许「直接 streamlit run 本文件」时找到同目录模块
_DIR = Path(__file__).resolve().parent
if str(_DIR) not in sys.path:
    sys.path.insert(0, str(_DIR))

import streamlit as st

from config import settings
from service import CustomerService


def _svc() -> CustomerService:
    """进程内单例：避免每次 rerun 重建图 / 连接池。"""
    _svc_ver = "media_project_v2"
    if (
        "cs_service" not in st.session_state
        or st.session_state.get("cs_service_ver") != _svc_ver
    ):
        st.session_state.cs_service = CustomerService()
        st.session_state.cs_service_ver = _svc_ver
    return st.session_state.cs_service


def _reset_chat(svc: CustomerService) -> None:
    """新建业务会话；thread_id 默认等于 session_id。"""
    st.session_state.session_id = svc.store.create_session()
    st.session_state.thread_id = st.session_state.session_id
    st.session_state.messages = []
    st.session_state.pending_clarify = False


def main() -> None:
    st.set_page_config(page_title="AI 客服", page_icon="💬", layout="wide")
    st.title("AI 客服")
    st.caption("意图分流 · Agent + Clarify/Tool Loop · Harness · MCP")

    svc = _svc()

    with st.sidebar:
        st.subheader("角色 / 意图")
        role_label = st.radio(
            "提问身份",
            options=["客户", "员工"],
            index=0 if st.session_state.get("role_hint", "customer") == "customer" else 1,
            horizontal=True,
        )
        role_hint = "customer" if role_label == "客户" else "employee"
        st.session_state.role_hint = role_hint
        auto_intent = st.checkbox(
            "自动识别意图（可覆盖侧栏）",
            value=bool(st.session_state.get("auto_intent", False)),
        )
        st.session_state.auto_intent = auto_intent
        st.caption("关闭自动识别时严格按侧栏身份分流。")

        st.subheader("连接状态")
        st.write(f"MCP: `{settings.mcp_url}`")
        st.write(f"项目(RAG): `{settings.project_id or '（未设置）'}`")
        st.write(
            f"素材项目: `{settings.media_project_id or settings.project_id or '（未设置）'}`"
        )
        st.write(
            f"型号覆盖: `{settings.llm_model or '（无，用中台项目配置）'}`"
        )
        st.write(f"工具循环上限: `{settings.max_tool_loops}`")
        st.write(f"追问上限: `{settings.max_clarify_loops}`")
        if not settings.project_id:
            st.warning("请在 models/ai_customer/.env 配置 PROJECT_ID")
        if not settings.mcp_client_token:
            st.warning(
                "请配置 MCP_CLIENT_TOKEN（与中台 MCP 鉴权一致，勿用项目 SERVICE_TOKEN）"
            )

        if st.button("新建会话", use_container_width=True):
            _reset_chat(svc)
            st.rerun()

        sessions = svc.store.list_sessions()
        if sessions:
            labels = {
                f"{s['title'] or '会话'} · {s['updated_at'][5:16]}": s["id"]
                for s in sessions
            }
            choice = st.selectbox("历史会话", options=list(labels.keys()))
            picked = labels[choice]
            if st.session_state.get("session_id") != picked:
                if st.button("加载该会话", use_container_width=True):
                    st.session_state.session_id = picked
                    meta = svc.store.get_session(picked) or {}
                    # 必须恢复 thread_id，否则无法 resume 旧 interrupt
                    st.session_state.thread_id = meta.get("thread_id") or picked
                    rows = svc.store.list_messages(picked)
                    st.session_state.messages = [
                        {
                            "role": m.role,
                            "content": m.content,
                            "sources": m.sources,
                            "trace_id": m.trace_id,
                        }
                        for m in rows
                    ]
                    st.session_state.pending_clarify = False
                    st.rerun()

    # ---- 初始化会话态 ----
    if "session_id" not in st.session_state:
        _reset_chat(svc)
    if "thread_id" not in st.session_state:
        st.session_state.thread_id = st.session_state.session_id
    if "messages" not in st.session_state:
        st.session_state.messages = []
    if "pending_clarify" not in st.session_state:
        st.session_state.pending_clarify = False

    if st.session_state.pending_clarify:
        pending_type = ""
        for m in reversed(st.session_state.messages):
            if m.get("interrupt_type"):
                pending_type = str(m["interrupt_type"])
                break
        if pending_type == "quote_offer":
            st.info("配置方案已推送：请回复是否需要具体报价")
        elif pending_type == "quote_contact":
            st.info("请留下姓名与联系方式，以便销售跟进报价")
        else:
            st.info("需要补充信息后继续（Clarify Loop）")

    # ---- 渲染历史气泡 ----
    for msg in st.session_state.messages:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])
            if msg.get("meta"):
                st.caption(msg["meta"])
            if msg.get("trace_id"):
                st.caption(f"trace_id: `{msg['trace_id']}`")
            if msg.get("sources"):
                try:
                    src = json.loads(msg["sources"])
                except Exception:
                    src = None
                if src:
                    with st.expander("知识库引用"):
                        for i, s in enumerate(src, 1):
                            if s.get("kind") == "attachments":
                                continue
                            sim = s.get("similarity")
                            sim_txt = (
                                f"{sim:.2f}" if isinstance(sim, (int, float)) else "-"
                            )
                            st.markdown(
                                f"**{i}. {s.get('filename')}** (相似度 {sim_txt})"
                            )
                            st.text(s.get("preview") or "")
            if msg.get("attachments"):
                try:
                    atts = (
                        json.loads(msg["attachments"])
                        if isinstance(msg["attachments"], str)
                        else msg["attachments"]
                    )
                except Exception:
                    atts = None
                if atts:
                    for a in atts:
                        title = a.get("title") or a.get("asset_id") or "素材"
                        st.caption(title)
                        if a.get("caption"):
                            st.caption(a["caption"])
                        url = a.get("url")
                        if a.get("type") == "video" and url:
                            st.video(url)
                        elif url:
                            st.image(url, use_container_width=True)

    pending_type = ""
    if st.session_state.pending_clarify:
        for m in reversed(st.session_state.messages):
            if m.get("interrupt_type"):
                pending_type = str(m["interrupt_type"])
                break
    if pending_type == "quote_offer":
        placeholder = "回复「需要」并留下姓名电话，或「不需要」…"
    elif pending_type == "quote_contact":
        placeholder = "请输入姓名与联系方式…"
    elif st.session_state.pending_clarify:
        placeholder = "请补充工艺/业务信息…"
    else:
        placeholder = "请输入问题"
    prompt = st.chat_input(placeholder)
    if prompt:
        # 以图 checkpoint 为准：若仍中断则必须 resume，避免把「需要」当成新问题重跑方案
        graph_pending = False
        try:
            snap = svc.get_state(st.session_state.thread_id)
            graph_pending = bool(snap.get("interrupted"))
            if graph_pending and not st.session_state.pending_clarify:
                st.session_state.pending_clarify = True
        except Exception:
            pass

        is_clarify = bool(st.session_state.pending_clarify or graph_pending)
        st.session_state.messages.append({"role": "user", "content": prompt})
        if is_clarify:
            st.toast("已收到补充，正在继续处理…", icon="⏳")

        history = [
            (m["role"], m["content"])
            for m in st.session_state.messages[:-1]
            if m["role"] in ("user", "assistant")
        ]

        status_label = (
            "Clarify / 报价留资恢复中…"
            if is_clarify
            else "意图分流 / Agent / 工具调用中…"
        )
        try:
            with st.status(status_label, expanded=True) as status:
                if is_clarify:
                    status.write("调用 resume_clarify，从断点继续图…")
                    result = svc.resume_clarify(
                        st.session_state.thread_id, prompt
                    )
                else:
                    status.write("调用 ask，启动新一轮图…")
                    result = svc.ask(
                        prompt,
                        session_id=st.session_state.session_id,
                        thread_id=st.session_state.thread_id,
                        history=history,
                        role_hint=st.session_state.role_hint,
                        auto_intent=st.session_state.auto_intent,
                        persist=True,
                    )
                if result.interrupted:
                    status.update(
                        label="仍需补充信息", state="complete", expanded=False
                    )
                else:
                    status.update(
                        label="本轮处理完成", state="complete", expanded=False
                    )
        except Exception as exc:
            st.error(f"问答失败: {exc}")
            st.session_state.messages.append(
                {
                    "role": "assistant",
                    "content": f"处理失败：{exc}",
                    "meta": "error",
                }
            )
            st.rerun()

        if result.thread_id:
            st.session_state.thread_id = result.thread_id
        if result.session_id:
            st.session_state.session_id = result.session_id

        meta_bits = [f"角色: {result.role}"]
        interrupt_type = ""
        if result.interrupted:
            meta_bits.append("状态: 待补充")
            st.session_state.pending_clarify = True
            if isinstance(result.interrupt_payload, dict):
                interrupt_type = str(
                    result.interrupt_payload.get("type") or ""
                )
        else:
            st.session_state.pending_clarify = False
            meta_bits.append(f"tool×{result.tool_round}")
        if result.degraded:
            meta_bits.append("degraded")

        st.session_state.messages.append(
            {
                "role": "assistant",
                "content": result.answer,
                "sources": json.dumps(
                    result.sources_summary, ensure_ascii=False
                ),
                "attachments": json.dumps(
                    result.attachments, ensure_ascii=False
                )
                if result.attachments
                else None,
                "trace_id": result.trace_id,
                "meta": " · ".join(meta_bits),
                "interrupt_type": interrupt_type,
            }
        )
        # 按 messages 整页重绘，避免 chat_input 同轮渲染像「没反应」
        st.rerun()


if __name__ == "__main__":
    main()
