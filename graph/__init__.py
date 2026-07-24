"""
graph 包入口（graph/__init__.py）
================================

对外导出：
  build_graph   — 编译客服状态机（见 build.py）
  CustomerState — 图状态 TypedDict（见 state.py）

节点实现见 nodes.py；流程说明见 ../FLOW_ANALYSIS.md。
"""

from graph.build import build_graph
from graph.state import CustomerState

__all__ = ["CustomerState", "build_graph"]
