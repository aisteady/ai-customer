"""
编译客服 LangGraph（graph/build.py）
====================================

学习要点
--------
1. 节点函数在 nodes.make_nodes 里定义；本文件只负责「连线」。
2. checkpointer 必须有：Clarify 用 interrupt 暂停后，resume 靠 thread_id 恢复。
   - 优先 PostgresSaver（与业务库同机，schema 见 CHECKPOINT_SCHEMA）
   - 失败且 ALLOW_MEMORY_CHECKPOINT=true 时回退 MemorySaver（进程重启会丢中断态）

拓扑（与 FLOW_ANALYSIS.md 一致）：

  START → route_intent
            ├─ employee → employee_answer → finalize → END
            └─ customer → agent_plan
                            ├─ clarify ↺→ agent_plan | run_tools
                            └─ run_tools → judge
                                  │         缺五要素 ↺ clarify
                                  ├─ quote_offer → quote_collect ↺ → finalize
                                  ├─ finalize
                                  ├─ agent_plan（未满 MAX_TOOL_LOOPS）
                                  └─ fallback_answer → quote_offer? → finalize → END

读代码：先扫 add_edge / add_conditional_edges，再下钻 nodes.py 各函数。
"""

from __future__ import annotations

import logging

from langgraph.graph import END, START, StateGraph

from harness import CustomerHarness
from llm import McpChat
from store import ChatStore
from tools import CustomerTools

from graph.nodes import NodeContext, make_nodes
from graph.state import CustomerState

logger = logging.getLogger(__name__)

# 进程内单例：避免 Streamlit 热重载时反复建连接池
_CHECKPOINTER = None
_POOL = None


def _make_checkpointer():
    """创建（或复用）LangGraph checkpointer。"""
    global _CHECKPOINTER, _POOL
    if _CHECKPOINTER is not None:
        return _CHECKPOINTER

    from config import settings

    try:
        import psycopg
        from langgraph.checkpoint.postgres import PostgresSaver
        from psycopg_pool import ConnectionPool

        from db import build_dsn

        schema = settings.checkpoint_schema
        with psycopg.connect(build_dsn()) as conn:
            conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
            conn.commit()

        # search_path 指到 checkpoint schema，避免污染 public
        dsn = build_dsn()
        if "?" in dsn:
            dsn_opts = f"{dsn}&options=-csearch_path%3D{schema},public"
        else:
            dsn_opts = f"{dsn}?options=-csearch_path%3D{schema},public"

        _POOL = ConnectionPool(
            conninfo=dsn_opts,
            max_size=max(2, settings.db_pool_max_size),
            kwargs={"autocommit": True, "prepare_threshold": 0},
            open=True,
        )
        checkpointer = PostgresSaver(_POOL)
        checkpointer.setup()
        _CHECKPOINTER = checkpointer
        logger.info("LangGraph checkpoint schema=%s", schema)
        return checkpointer
    except Exception as exc:
        if not settings.allow_memory_checkpoint:
            raise RuntimeError(f"Postgres checkpoint 初始化失败: {exc}") from exc
        logger.warning("回退 MemorySaver: %s", exc)
        from langgraph.checkpoint.memory import MemorySaver

        _CHECKPOINTER = MemorySaver()
        return _CHECKPOINTER


def build_graph(
    store: ChatStore,
    harness: CustomerHarness,
    *,
    llm: McpChat,
    tools: CustomerTools,
    employee_system: str = "",
):
    """
    组装并 compile 客服图。

    employee_system：员工路径 system prompt（service 启动时已解析中台/本地默认）。
    """
    from prompts import EMPLOYEE_SYSTEM

    ctx = NodeContext(
        store=store,
        harness=harness,
        llm=llm,
        tools=tools,
        employee_system=employee_system or EMPLOYEE_SYSTEM,
    )
    nodes = make_nodes(ctx)

    g = StateGraph(CustomerState)
    g.add_node("route_intent", nodes["route_intent"])
    g.add_node("employee_answer", nodes["employee_answer"])
    g.add_node("agent_plan", nodes["agent_plan"])
    g.add_node("clarify", nodes["clarify"])
    g.add_node("run_tools", nodes["run_tools"])
    g.add_node("judge", nodes["judge"])
    g.add_node("fallback_answer", nodes["fallback_answer"])
    g.add_node("quote_offer", nodes["quote_offer"])
    g.add_node("quote_collect", nodes["quote_collect"])
    g.add_node("finalize", nodes["finalize"])

    # ---- 连线 ----
    g.add_edge(START, "route_intent")
    g.add_conditional_edges(
        "route_intent",
        nodes["route_by_role"],
        {"employee": "employee_answer", "customer": "agent_plan"},
    )
    g.add_edge("employee_answer", "finalize")
    g.add_conditional_edges(
        "agent_plan",
        nodes["route_after_plan"],
        {"clarify": "clarify", "run_tools": "run_tools"},
    )
    g.add_conditional_edges(
        "clarify",
        nodes["route_after_clarify"],
        {"agent_plan": "agent_plan", "run_tools": "run_tools"},
    )
    g.add_conditional_edges(
        "run_tools",
        nodes["route_after_tools"],
        {"clarify": "clarify", "judge": "judge"},
    )
    g.add_conditional_edges(
        "judge",
        nodes["route_after_judge"],
        {
            "finalize": "finalize",
            "quote_offer": "quote_offer",
            "fallback": "fallback_answer",
            "agent_plan": "agent_plan",
        },
    )
    g.add_conditional_edges(
        "fallback_answer",
        nodes["route_after_fallback"],
        {"quote_offer": "quote_offer", "finalize": "finalize"},
    )
    g.add_conditional_edges(
        "quote_offer",
        nodes["route_after_quote_offer"],
        {"quote_collect": "quote_collect", "finalize": "finalize"},
    )
    g.add_conditional_edges(
        "quote_collect",
        nodes["route_after_quote_collect"],
        {"quote_collect": "quote_collect", "finalize": "finalize"},
    )
    g.add_edge("finalize", END)

    return g.compile(checkpointer=_make_checkpointer())
