# 智能客服（ai_customer）

> **一句话**：面向企业的对话式 AI 客服。  
> 内部员工问制度/知识库 → 直接检索回答；  
> 外部客户问产品、要看图、要工艺选型、要留资报价 → 由 Agent 自动选工具、追问补全、再回答。

本应用 **不持有** 通义 API Key，检索与大模型一律走 **AI 数据中台的 MCP**。  
工艺方案走同仓 **工艺选型** 的 HTTP 接口；图片/视频走中台 MCP `search_media`。

如果你是零基础，建议顺序：先读本文 → 按「第一次跑起来」操作 → 再需要时看文末「术语小词典」和 `FLOW_ANALYSIS.md`。

---

## 目录

1. [它能干什么（场景举例）](#1-它能干什么场景举例)
2. [和中台 / 其它项目的关系](#2-和中台--其它项目的关系)
3. [两种用户，两条路径](#3-两种用户两条路径)
4. [客户路径里有哪些「工具」](#4-客户路径里有哪些工具)
5. [对话是怎么一步步跑的](#5-对话是怎么一步步跑的)
6. [第一次跑起来](#6-第一次跑起来)
7. [环境变量说明](#7-环境变量说明)
8. [界面怎么用](#8-界面怎么用)
9. [目录结构（看代码时用）](#9-目录结构看代码时用)
10. [常见问题](#10-常见问题)
11. [术语小词典](#11-术语小词典)
12. [更深入的文档](#12-更深入的文档)

---

## 1. 它能干什么（场景举例）

| 用户说… | 系统大致会… |
|---------|-------------|
| 「报销要找谁审批？」（员工） | 检索内部知识库，按文档片段回答 |
| 「立磨有什么优势？」（客户） | 检索产品文档，组织话术回答 |
| 「看看立磨的图片」 | 强制调用素材检索，命中后聊天窗口直接展示图片 |
| 「石英砂要磨到 325 目…」 | 调工艺选型接口；缺参数就追问；齐了就出配置方案 |
| 「这个方案要报价」 | 收集姓名 + 手机/邮箱，写入 CRM 线索表 |

**不会做的事（设计边界）**：

- 不自己发明价格、交期、合同条款  
- 不直连向量数据库（一律 MCP）  
- 不在客服里维护工艺 BOM 规则（那是 `ai_quotation`）  
- 不替代公司完整 CRM 产品（目前是线索落库，可后续对接）

---

## 2. 和中台 / 其它项目的关系

```text
┌─────────────────────────────┐
│  你打开的客服页面（Streamlit） │
│  models/ai_customer/app.py   │
└──────────────┬──────────────┘
               │
               ▼
┌─────────────────────────────┐
│  CustomerService + LangGraph │  ← 本目录核心：编排对话
└───────┬───────────┬─────────┘
        │           │
        │ MCP       │ HTTP
        ▼           ▼
   中台 :8765    工艺选型 :8510
   搜文档/素材     /api/v1/recommend
   调大模型
```

| 依赖 | 是否必须 | 说明 |
|------|----------|------|
| 中台 API + MCP | **必须** | 没有 MCP，客服无法检索和调模型 |
| PostgreSQL | **强烈建议** | 存会话、消息、追问断点、CRM；开发可临时内存断点 |
| `ai_quotation` 的 `api.py` | 可选 | 不做工艺选型可先不启；工具会降级/提示 |
| 素材已发布 | 可选 | 要测发图，需在中台「素材管理」发布过素材 |

仓库总览见：[../../README.md](../../README.md)

---

## 3. 两种用户，两条路径

系统会先判断（或由你在侧栏指定）提问者是 **员工** 还是 **客户**：

```text
用户提问
   │
   ▼
意图分流 route_intent
   ├─ employee（员工）──► 只做知识库检索 ──► 直接回答 ──► 结束
   │                     （短、稳、可预期）
   │
   └─ customer（客户）──► Agent 规划
                            ├─ 信息不够 → 追问 Clarify（可多轮）
                            └─ 信息够了 → 调工具 → 质检
                                  ├─ 满意 → 组织答复（可能带图）
                                  │         若有配置方案 → 问是否报价 → 留资
                                  └─ 不满意 → 再试工具（有次数上限）
                                        └─ 用尽 → Fallback 兜底 / 建议转人工
```

为什么要分两条？

- **员工 FAQ** 要稳：少花样，严格依据知识库  
- **客户咨询** 要能动：可能要组合「搜文档 + 搜图 + 工艺推荐」

---

## 4. 客户路径里有哪些「工具」

Agent 不会「想干什么就干什么」，只能从下面三个工具里选：

| 工具名 | 干什么 | 背后调用 |
|--------|--------|----------|
| `rag_search` | 搜文字知识库 | MCP `search_documents`（用 `PROJECT_ID`） |
| `search_media` | 搜图片/视频 | MCP `search_media`（用 `MEDIA_PROJECT_ID`） |
| `process_config_recommend` | 工艺配置推荐 | HTTP → `ai_quotation` `/api/v1/recommend` |

**新手易错点**：

- 文档项目 ID（`PROJECT_ID`）和素材项目 ID（`MEDIA_PROJECT_ID`）**可以不同**  
- 用户说「看看」「发张图」时，系统会 **强制带上** `search_media`，避免 Agent 只搜文字  
- 没搜到图时，回答 **不应** 假装「已经发图了」（提示词里有约束）

---

## 5. 对话是怎么一步步跑的

用「客户要看立磨图片」举例：

1. 你在页面输入：「看看立磨的图片」  
2. 系统判定角色 = 客户  
3. Agent 规划：工具里必须有 `search_media`（也可同时 `rag_search`）  
4. 执行工具：MCP 去中台搜已发布素材  
5. 质检：有命中 → 生成简短文字说明  
6. 页面渲染：文字气泡 + 图片附件（签名 URL）  
7. 全过程有 `trace_id`，方便在中台追踪里排查  

用「工艺选型」举例：

1. 用户描述需求，但缺「产量」等五要素之一  
2. 工艺 API 返回 `ok=false` + `missing`  
3. 客服进入 **Clarify**：只问缺的那几项（界面会出现待补充状态）  
4. 你补充后，系统 **恢复同一条对话图**，再调一次推荐 API  
5. 出方案后，可询问是否需要报价 → 收集联系方式 → 写入 `crm_leads`

「追问中途关掉页面再打开」能续上，靠的是 LangGraph **Checkpoint**（断点存数据库）。

---

## 6. 第一次跑起来

### 6.1 先启动中台（仓库根目录）

```bash
# 终端 A/B/C
uv run python start_api.py
uv run python start_ui.py
uv run python start_mcp.py
```

在运维台创建/确认：

1. 有一个 **文档知识库项目**（记下 UUID → 填客服 `PROJECT_ID`）  
2. （可选）有 **多模态素材项目**，并已上传、标注、**发布**至少一张图  
3. `.env` 里 `MCP_CLIENT_TOKEN` 已设置（与客服保持一致）

### 6.2 （可选）启动工艺推荐 API

```bash
cd models/ai_quotation
uv sync
uv run python api.py
# 默认 http://127.0.0.1:8510
```

### 6.3 启动客服

```bash
cd models/ai_customer
cp .env.example .env
# 编辑 .env：至少填 PROJECT_ID、MCP_CLIENT_TOKEN；要测图则确认 MEDIA_PROJECT_ID
uv sync
uv run streamlit run app.py
```

浏览器打开 Streamlit 提示的地址（常见为 `http://localhost:8501`；若与中台 UI 端口冲突，按终端实际端口为准）。

也可用命令行聊天：

```bash
uv run python cli_chat.py
```

---

## 7. 环境变量说明

复制 `.env.example` 为 `.env`。常用项：

| 变量 | 含义 | 示例 / 注意 |
|------|------|-------------|
| `PROJECT_ID` | 文档 RAG 用的中台项目 | 必填，UUID |
| `MEDIA_PROJECT_ID` | 素材检索用的项目 | 默认可指向多模态素材中心 |
| `MCP_URL` | MCP 地址 | `http://127.0.0.1:8765/mcp` |
| `MCP_CLIENT_TOKEN` | 调 MCP 的密钥 | **必须与中台一致**；不是项目 SERVICE_TOKEN |
| `TOP_K` / `SEARCH_THRESHOLD` | 文档检索条数与阈值 | 默认 5 / 0.45 |
| `MEDIA_SEARCH_THRESHOLD` | 素材检索阈值 | 默认 0.25（可略宽） |
| `MAX_TOOL_LOOPS` | 工具循环硬顶 | 默认 8；打满会 degraded 兜底 |
| `MAX_CLARIFY_LOOPS` | 追问硬顶 | 默认 10 |
| `APP_DB_SCHEMA` | 本应用表所在 schema | 默认 `ai_customer` |
| `ALLOW_MEMORY_CHECKPOINT` | 无库时用内存断点 | 开发可 true；生产建议 false |
| `PROCESS_CONFIG_URL` | 工艺 API | 默认 `http://127.0.0.1:8510` |
| `PROCESS_CONFIG_TOKEN` | 工艺 API 鉴权 | 与报价侧 `CUSTOMER_API_TOKEN` 一致（若启用） |
| `LLM_MODEL` | 指定模型名 | 留空则用中台项目配置 |

数据库：可填 `DATABASE_URL`，或不填而继承仓库根 `.env` 的 `DB_*`。

---

## 8. 界面怎么用

侧栏常见项：

- **角色**：自动识别 / 强制客户 / 强制员工  
- **会话列表**：历史聊天；加载时会恢复 `thread_id`，以便继续未完成的追问  
- **配置摘要**：当前 `PROJECT_ID`、素材项目、MCP 地址等（便于排错）

主区域：

- 输入问题发送  
- 若系统在追问，按提示补充即可（不要当成全新无关问题乱开话题）  
- 有素材命中时，气泡下方会显示图片/视频附件  
- 底部可能显示 `角色 · tool×N · degraded` 与 `trace_id`

---

## 9. 目录结构（看代码时用）

```text
ai_customer/
├── app.py              # Streamlit 界面（尽量不写业务分支）
├── cli_chat.py         # 命令行聊天
├── service.py          # 对外门面：ask / resume_clarify
├── config.py           # 读 .env
├── harness.py          # 轮数硬顶 + 审计事件
├── graph/
│   ├── build.py        # 组装状态机
│   ├── nodes.py        # 各节点逻辑（意图、规划、工具、报价…）
│   └── state.py        # 状态字段定义
├── tools.py            # 三个工具的具体实现
├── rag.py / media.py   # 文档检索 / 素材检索封装
├── mcp_client.py       # 连接中台 MCP
├── llm.py              # 经 MCP 调大模型
├── crm.py              # 线索写入
├── prompts.py          # 各节点提示词（改话术优先改这里）
├── db.py / store.py    # 数据库与会话存储
└── FLOW_ANALYSIS.md    # 流程深挖（进阶）
```

设计原则（方便你改代码时不踩坑）：

1. **UI 零业务分支** — 页面只调 `CustomerService`  
2. **图内是业务，Harness 是护栏** — 复杂 if/else 在节点里；硬顶在 Harness  
3. **工具契约稳定** — Agent 只认工具名；换实现改 `tools.py` 即可  

---

## 10. 常见问题

**Q：页面能聊，但一直说知识库没有 / 搜不到图？**  
A：检查 MCP 是否启动；`PROJECT_ID` / `MEDIA_PROJECT_ID` 是否指对项目；素材是否已 **发布**（仅上传未发布不可检索）；改完中台 MCP 代码后是否 **重启了 MCP**。

**Q：显示 `tool×8 · degraded`？**  
A：工具循环打满硬顶。常见原因：工具一直报错（如 MCP 连错地址）、或质检一直认为不够。先看 `trace_id` 与 MCP/API 日志。

**Q：追问补充后方案乱了 / 像重新开始？**  
A：确认加载的是同一会话，且 `thread_id` 被正确恢复；不要在 Clarify 未完成时新开无关会话硬聊。

**Q：要配工艺但一直缺参数？**  
A：先确认 `uv run python api.py`（工艺）已启动；五要素由工艺侧校验，客服只负责把 `missing` 问出来。

**Q：大模型报错 / 没密钥？**  
A：密钥配在 **中台根目录** `.env` 的 `DASHSCOPE_API_KEY`，不是客服目录。

---

## 11. 术语小词典

| 词 | 白话解释 |
|----|----------|
| Agent | 会「先想再调用工具」的规划器，不是固定死脚本 |
| Clarify | 信息不够时向用户追问补全 |
| Tool Loop | 调工具 → 看结果 → 不够再调，循环有上限 |
| Fallback | 工具试尽后的兜底回答 |
| Harness | 外面的安全带：限制次数、记审计日志 |
| MCP | 中台提供的标准工具接口 |
| Checkpoint | 对话图的存档，用于打断后继续 |
| session_id | 聊天记录 ID（气泡列表） |
| thread_id | 状态机存档 ID（追问恢复用） |
| degraded | 降级：未完美完成（如打满轮次）仍给出当前最佳答复 |
| attachments | 附件列表（图片/视频的签名 URL 等） |

---

## 12. 更深入的文档

- 流程与节点细节：[FLOW_ANALYSIS.md](./FLOW_ANALYSIS.md)  
- **客服如何与工艺选型通信（代码 + 设计取舍）**：[PROCESS_CONFIG_COMM.md](./PROCESS_CONFIG_COMM.md)  
- 数据中台总览：[../../README.md](../../README.md)  
- 素材中心：[../ai_mutli_base/README.md](../ai_mutli_base/README.md)  
- 工艺选型：[../ai_quotation/README.md](../ai_quotation/README.md)  
