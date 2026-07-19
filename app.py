"""
AI 客服 — Streamlit 主界面
==========================

运行：
  uv run streamlit run models/ai_customer/app.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_DIR = Path(__file__).resolve().parent
if str(_DIR) not in sys.path:
    sys.path.insert(0, str(_DIR))

import streamlit as st

from config import settings
from service import CustomerService


def _svc() -> CustomerService:
    if "cs_service" not in st.session_state:
        st.session_state.cs_service = CustomerService()
    return st.session_state.cs_service


def main() -> None:
    st.set_page_config(page_title="AI 客服", page_icon="💬", layout="wide")
    st.title("AI 客服")
    st.caption("基于 AI数据中台 MCP（Streamable HTTP）混合检索 + DashScope 大模型")

    svc = _svc()

    with st.sidebar:
        st.subheader("连接状态")
        st.write(f"MCP: `{settings.mcp_host}:{settings.mcp_port}`")
        st.write(f"项目: `{settings.project_id or '（未设置 PROJECT_ID）'}`")
        st.write(f"模型: `{settings.llm_model}`")
        st.write(
            "LLM Key: "
            + ("已配置" if settings.dashscope_api_key else "未配置（将降级为仅召回）")
        )
        if not settings.project_id:
            st.warning("请在 models/ai_customer/.env 配置 PROJECT_ID")
        if st.button("新建会话", use_container_width=True):
            st.session_state.session_id = svc.store.create_session()
            st.session_state.messages = []
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
                    st.rerun()

    if "session_id" not in st.session_state:
        st.session_state.session_id = svc.store.create_session()
    if "messages" not in st.session_state:
        st.session_state.messages = []

    for msg in st.session_state.messages:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])
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
                            sim = s.get("similarity")
                            sim_txt = f"{sim:.2f}" if isinstance(sim, (int, float)) else "-"
                            st.markdown(
                                f"**{i}. {s.get('filename')}** (相似度 {sim_txt})"
                            )
                            st.text(s.get("preview") or "")

    prompt = st.chat_input("请输入问题，例如：考勤请假怎么申请？")
    if prompt:
        st.session_state.messages.append({"role": "user", "content": prompt})
        with st.chat_message("user"):
            st.markdown(prompt)

        history = [
            (m["role"], m["content"])
            for m in st.session_state.messages[:-1]
            if m["role"] in ("user", "assistant")
        ]
        with st.chat_message("assistant"):
            with st.spinner("正在检索知识库并生成回答…"):
                try:
                    result = svc.ask(
                        prompt,
                        session_id=st.session_state.session_id,
                        history=history,
                        persist=True,
                    )
                except Exception as exc:
                    st.error(f"问答失败: {exc}")
                    return

            st.markdown(result.answer)
            if result.degraded:
                st.warning("当前为降级模式（召回失败或未配置/调用 LLM 失败）")
            tid = result.retrieval.trace_id
            if tid:
                st.caption(f"trace_id: `{tid}`（可在中台「链路追踪」查看）")
            if result.sources_summary:
                with st.expander("知识库引用"):
                    for i, s in enumerate(result.sources_summary, 1):
                        sim = s.get("similarity")
                        sim_txt = f"{sim:.2f}" if isinstance(sim, (int, float)) else "-"
                        st.markdown(f"**{i}. {s.get('filename')}** (相似度 {sim_txt})")
                        st.text(s.get("preview") or "")

            st.session_state.messages.append(
                {
                    "role": "assistant",
                    "content": result.answer,
                    "sources": json.dumps(result.sources_summary, ensure_ascii=False),
                    "trace_id": tid,
                }
            )


if __name__ == "__main__":
    main()
