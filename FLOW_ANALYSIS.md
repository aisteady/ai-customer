# AI 客服问答流程分析

本文基于当前 `models/ai_customer` 实现，梳理**员工**与**客户**两条问答路径：从 UI/CLI 入口到落库结束，以及 Agent、Clarify Loop、Tool Loop、Harness 各自职责。

---

## 1. 总览

```text
用户提问（Streamlit / CLI）
  → CustomerService.ask()  （Harness.wrap_run 包一层）
  → LangGraph：route_intent
       ├─ role=employee → employee_answer → finalize → END
       └─ role=customer → agent_plan → …
                              ├─ 信息不足 → clarify（interrupt）↺
                              └─ 信息足够 → run_tools → judge
                                    ├─ 满意 → finalize
                                    ├─ 不满意且 tool < MAX_TOOL_LOOPS → 再 agent_plan
                                    └─ 已满 MAX_TOOL_LOOPS → fallback_answer → finalize
```

| 维度 | 员工路径 | 客户路径 |
|------|----------|----------|
| 决策 | 无 Agent，固定管道 | Agent 选工具、判信息是否够 |
| 检索 | RAG 一次 | 按 plan 可多次、可换工具 |
| 追问 | 无 | Clarify `interrupt`，默认最多 10 轮（`MAX_CLARIFY_LOOPS`） |
| 重试 | 无 | Tool 最多 8 轮（`MAX_TOOL_LOOPS`），否则 fallback |
| Prompt | 企业知识库助手 | 客户客服 + 规划 / 质检 / 兜底 |
| 出口 | 一律 `finalize` | 同上 |

核心代码：

| 层级 | 文件 |
|------|------|
| 入口 | `app.py` / `cli_chat.py` → `service.py` |
| 守卫 | `harness.py` |
| 图 | `graph/build.py`、`graph/nodes.py`、`graph/state.py` |
| 工具 | `tools.py`（`rag_search` + `process_config_recommend` stub） |
| 文案 | `prompts.py` |

---

## 2. 共同入口

### 2.1 UI / CLI

1. 用户输入问题。
2. 侧栏（或 CLI 命令）提供：
   - `role_hint`：`customer` | `employee`
   - `auto_intent`：是否让 LLM 覆盖侧栏身份
3. 若上一轮处于 Clarify 中断（`pending_clarify`），本轮走 `resume_clarify`，否则走 `ask`。

### 2.2 Service + Harness

`CustomerService.ask()` / `resume_clarify()` 均经 `CustomerHarness.wrap_run`：

- 写 `harness_events`：`run_start` → 成功 `run_ok` / 失败 `run_error`
- 图内另有轮数硬顶：`MAX_CLARIFY_LOOPS`（默认 10）、`MAX_TOOL_LOOPS`（默认 8）

### 2.3 意图路由 `route_intent`

1. 取 `role_hint`；非法则默认 `customer`。
2. **`auto_intent=False`**：最终角色 = 侧栏，事件 `intent_forced`。
3. **`auto_intent=True`** 且 LLM 可用：按 `INTENT_SYSTEM` 分类为 `customer|employee`，事件 `intent_classified`；失败则退回侧栏，事件 `intent_fallback`。
4. 用户问题写入 PostgreSQL `ai_customer.messages`。
5. 条件边 `route_by_role` → 员工或客户子图。

---

## 3. 员工路径（端到端）

```text
route_intent → employee_answer → finalize → END
```

适合：制度、考勤、报销、内部 FAQ 等「查知识库即可答」的问题。

### 3.1 `employee_answer`

1. 调用 `rag_search` → MCP `search_documents`（`project_id` / `top_k` / `threshold`）。
2. 组装上下文：召回片段 + 多轮 `history`。
3. System prompt 优先级：
   - 启动时 `resolve_system_prompt()`：中台 `get_project_prompt(name=system)`（约 60s 缓存）
   - 失败则用本地 `EMPLOYEE_SYSTEM`（企业知识库助手，约束「只依据片段、不足则说明无法确定」）。
4. MCP `chat_completion` 生成答复。
5. 降级：
   - 无 `PROJECT_ID`：直接展示召回原文（`degraded=True`）
   - LLM 失败：错误说明 + 召回原文

本路径**不**进入 Agent、Clarify、Tool 重试。

### 3.2 `finalize`

1. 写入助手消息（`sources` JSON、`trace_id`）。
2. Harness 事件 `finalize`（含 role、轮数等）。
3. 图结束；UI 展示答案与知识库引用。

### 3.3 员工路径时序（示意）

```text
用户: 「请假流程是什么？」
  → ask(role_hint=employee, auto_intent=false)
  → route_intent → role=employee
  → MCP search_documents
  → MCP chat_completion（员工 prompt + 片段）
  → 落库 assistant + sources
  → UI 展示
```

---

## 4. 客户路径（端到端）

```text
route_intent
  → agent_plan
       ├─ info_enough=false → clarify ↺ → agent_plan …
       └─ info_enough=true  → run_tools → judge
              ├─ answer_ok → finalize
              ├─ !ok && tool_round < MAX_TOOL_LOOPS → agent_plan …
              └─ tool_round >= MAX_TOOL_LOOPS → fallback_answer → finalize
```

适合：产品咨询、售后口径、工艺/选型推荐（本阶段工艺工具为 stub）等需要「选工具 + 可能补参」的问题。

### 4.1 Agent 规划 `agent_plan`

按 `AGENT_PLAN_SYSTEM`，LLM 输出 JSON（解析失败则默认只调 `rag_search`）：

| 字段 | 含义 |
|------|------|
| `tools` | `rag_search` 和/或 `process_config_recommend` |
| `info_enough` | 当前信息是否够调工具 |
| `missing_info` | 缺什么 |
| `clarify_question` | 向客户追问的一句话 |
| `tool_query` | 实际检索/推荐用的查询短句 |

原则（提示词约定）：

- 纯 FAQ / 产品介绍 → 常只要 `rag_search`
- 选型、配置、工艺方案 → 倾向 `process_config_recommend`；缺物料/细度/产量等则 `info_enough=false`
- 若 `clarify_round >= MAX_CLARIFY_LOOPS`：强制 `info_enough=true`，停止追问

### 4.2 Clarify Loop（信息不足）

**目标**：跨轮向用户要参数，状态靠 LangGraph Checkpoint 恢复。

1. `clarify_round + 1`；Harness `guard_clarify_round`。
2. **未超限**：
   - 助手追问写入 DB
   - `interrupt({ type: clarify, question, missing_info, round, ... })`
   - 图暂停；`ask` 返回 `interrupted=True`，UI 置 `pending_clarify`
3. 用户下一条输入 → `resume_clarify(thread_id, user_reply)`：
   - 补充合并进 `extras`
   - 用户补充写入 DB
   - 回到 `agent_plan` 重新规划
4. **已超限**：
   - 不再 `interrupt`
   - 标记 `info_enough=true`，带着已有信息进入工具

与 Tool Loop 分离：Clarify 是「问人」；Tool 是「调系统」。

### 4.3 Tool Loop（`run_tools` → `judge`）

**run_tools**

1. `tool_round + 1`；Harness `guard_tool_round`（硬顶 `MAX_TOOL_LOOPS`，默认 8）。
2. 按 plan 依次执行：
   - `rag_search`：真实 MCP 检索，汇总 `sources` / `trace_id`
   - `process_config_recommend`：**stub**，返回模拟配置建议文案（后续可接 `ai_quotation`）
3. 结果追加到 `tool_results`。
4. 若本轮已超硬顶：不调工具，标记错误，交给后续 judge → fallback。

**judge**

1. 将「客户补充 + 全部工具结果」交给 `JUDGE_SYSTEM`。
2. 输出：`answer_ok`、可选 `draft_answer`、`reason`。
3. 路由：
   - `answer_ok`（或已 `status=done`）→ `finalize`
   - 不满意且 `tool_round < MAX_TOOL_LOOPS` → 再 `agent_plan`（可换工具或再 Clarify）
   - `tool_round >= MAX_TOOL_LOOPS` → `fallback_answer`

### 4.4 Fallback `fallback_answer`

工具用尽仍不够时：

1. 用 `FALLBACK_SYSTEM` + 已收集上下文，尽力生成最终答复。
2. 标明不确定处，建议补充或转人工；`degraded=True`。
3. 进入 `finalize`。

设计意图：避免无限 ReAct；工具轮次用尽后带已有信息直出 LLM。

### 4.5 `finalize`（与员工相同职责）

落库助手消息、sources、`trace_id`；Harness `finalize`；END。

### 4.6 客户路径示例

**A. 信息一次够（无追问）**

```text
用户: 「你们除尘器质保多久？」
  → route → customer → agent_plan（rag_search, info_enough=true）
  → run_tools(rag) → judge(ok) → finalize
```

**B. 需补参（Clarify）**

```text
用户: 「帮我推荐一套磨粉配置」
  → agent_plan（process_config_recommend, info_enough=false）
  → clarify interrupt: 「请补充物料、细度、产量…」
  → 用户: 「碳酸钙，d95=10μm，2t/h」
  → resume → agent_plan → run_tools(stub) → judge → finalize
```

**C. 工具重试后仍不够（Fallback）**

```text
  → run_tools ×1 → judge(不够)
  → agent_plan → run_tools ×2 → judge(不够)
  → … 直至 tool_round 达 MAX_TOOL_LOOPS …
  → fallback_answer（带全部上下文）→ finalize
```

---

## 5. Harness 在两条路径中的位置

```text
┌─────────────────────────────────────┐
│  CustomerHarness.wrap_run            │  ← ask / resume_clarify
│  ┌───────────────────────────────┐  │
│  │ LangGraph（业务状态机）         │  │
│  │  + guard_clarify_round        │  │
│  │  + guard_tool_round           │  │
│  └───────────────────────────────┘  │
│  emit → harness_events               │
└─────────────────────────────────────┘
```

| 能力 | 说明 |
|------|------|
| `wrap_run` | 单次业务调用审计（耗时、错误） |
| 轮数硬顶 | 与配置项绑定，防止追问/工具跑飞 |
| `emit` | 意图、规划、追问、工具、质检、兜底、结束等事件 |
| **不做** | 询报价那种后台 `HarnessLoop` 扫待审工单（客服无此模型） |

---

## 6. 数据与状态

### 6.1 业务库（schema `ai_customer`）

| 表 | 用途 |
|----|------|
| `sessions` | 会话；含 `role`、`thread_id` |
| `messages` | user/assistant；可带 `sources`、`trace_id` |
| `harness_events` | 审计事件 |

### 6.2 Checkpoint

- 默认与业务同 schema（`CHECKPOINT_SCHEMA`）
- Clarify `interrupt` 依赖 checkpoint 恢复；失败时可按 `ALLOW_MEMORY_CHECKPOINT` 回退内存（开发方便，生产慎用）

### 6.3 图状态要点（`CustomerState`）

`role` / `role_hint` / `auto_intent`、`question` / `extras`、`plan`、`tool_round` / `clarify_round`、`tool_results` / `sources`、`answer` / `answer_ok`、`status`、`degraded` 等。

---

## 7. 与中台、询报价的边界

| 系统 | 职责 |
|------|------|
| AI 数据中台 | 文档/向量、混合检索、项目提示词、大模型网关与 Token |
| `ai_customer` | 对话体验、意图分流、Agent/双 Loop/Harness、会话与审计 |
| `ai_quotation`（后续） | 正式工艺配置清单；可替换本应用 `process_config_recommend` stub |

本应用不持有 `DASHSCOPE_API_KEY`；型号可由中台项目配置，`.env` 中 `LLM_MODEL` 可选覆盖。

---

## 8. 配置开关（影响流程行为）

| 变量 | 默认 | 影响 |
|------|------|------|
| `MAX_TOOL_LOOPS` | `8` | 客户工具循环硬顶 |
| `MAX_CLARIFY_LOOPS` | `10` | 追问硬顶 |
| `TOP_K` / `SEARCH_THRESHOLD` | `5` / `0.45` | RAG 召回 |
| `ALLOW_MEMORY_CHECKPOINT` | 开发常 `true` | Checkpoint 降级策略 |
| `PROJECT_ID` / `MCP_*` | — | 无项目则员工/客户 LLM 与 RAG 能力降级 |

---

## 9. 小结

- **员工**：意图确定后走「检索 → 生成 → 落库」，路径短、行为可预期，适合内部知识问答。
- **客户**：在 Harness 硬顶内，用 Agent 决定工具与是否追问；Clarify 问人、Tool 调系统、工具轮次用尽仍不够则 fallback，避免无限循环。
- 两条路径共享 MCP、会话存储与 `finalize`；差异集中在 `route_intent` 之后的子图。

若后续将 stub 换成真实 `ai_quotation` 调用，客户路径的节点拓扑可不变，只需替换 `tools.process_config_recommend` 的实现。
