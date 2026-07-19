# AI Customer Service（智能客服）

基于 **RAG（检索增强生成）** 的企业知识库智能客服应用。  
通过 **MCP（Model Context Protocol）** 对接上游 AI 数据中台的混合检索能力，再调用 **通义千问（DashScope）** 生成有依据的客服回答；本地用 SQLite 持久化多轮会话。

> 本目录可整体拷贝为独立仓库使用，不依赖中台源码，仅依赖其已启动的 MCP / API 服务。

---

## 目录

1. [项目亮点（适合写进简历）](#1-项目亮点适合写进简历)
2. [业务背景与目标](#2-业务背景与目标)
3. [技术栈](#3-技术栈)
4. [系统架构](#4-系统架构)
5. [核心能力](#5-核心能力)
6. [问答流水线](#6-问答流水线)
7. [目录与模块说明](#7-目录与模块说明)
8. [环境配置](#8-环境配置)
9. [快速开始](#9-快速开始)
10. [运行模式说明](#10-运行模式说明)
11. [与数据中台的协作边界](#11-与数据中台的协作边界)
12. [扩展与二次开发建议](#12-扩展与二次开发建议)
13. [常见问题](#13-常见问题)
14. [简历描述参考（可直接改写）](#14-简历描述参考可直接改写)

---

## 1. 项目亮点（适合写进简历）

| 亮点 | 说明 |
|------|------|
| **RAG 客服闭环** | 检索 → 组上下文 → LLM 生成 → 引用展示 → 会话落库，完整可演示 |
| **MCP 协议解耦** | 客服作为「外部 AI 应用」，经 TCP JSON-RPC 调中台工具，不直连业务库 |
| **混合检索消费** | 调用中台 `search_documents`（向量 + 关键词 / RRF），而非自建向量库 |
| **可配置系统提示词** | 按 `project_id` 从中台拉取 `system` Prompt（短缓存），支持多租户话术 |
| **全链路可观测** | 每次召回携带 `trace_id`，可回中台「链路追踪」还原 MCP/HTTP/检索耗时 |
| **降级与容错** | 无 API Key、召回失败、LLM 失败时仍可返回可用结果，避免白屏崩溃 |
| **双入口** | Streamlit Web 聊天 + CLI 终端多轮，同一套 `CustomerService` 编排层 |
| **本地会话存储** | SQLite 存 sessions / messages（含 sources、trace_id），与中台 schema 解耦 |

---

## 2. 业务背景与目标

企业文档（制度、FAQ、产品说明）往往分散在 PDF / Word / 表格中。传统客服依赖人工翻资料，响应慢且口径不一。

本项目目标：

1. **用户用自然语言提问**，系统从已入库知识库召回相关片段；
2. **大模型仅依据召回内容作答**，减少幻觉，并展示引用来源；
3. **客服应用与知识中台分离**：中台管文档/向量/权限，客服管对话体验与会话；
4. **可按项目（租户）切换知识范围与系统提示词**，便于多业务线复用同一客服壳。

---

## 3. 技术栈

| 层级 | 技术 | 用途 |
|------|------|------|
| 交互层 | Streamlit / CLI | Web 聊天界面、终端调试 |
| 编排层 | Python 3.10+ | `CustomerService` 串联召回、Prompt、生成、落库 |
| 协议层 | 官方 MCP Streamable HTTP 客户端 | `MCP_URL` + Bearer 调用 tools |
| 检索层 | 上游 AI 数据中台 | 混合检索、项目 Prompt、链路追踪 |
| 生成层 | 阿里云 DashScope（Qwen） | 对话式生成客服话术 |
| 存储层 | SQLite | 本地会话与消息（含引用 JSON） |
| 配置 | python-dotenv | 本目录 `.env` + 可选继承中台根 `.env` |

**本应用直接依赖（运行时）：**

- `streamlit`、`python-dotenv`、`dashscope`、`requests`（经中台 MCP 间接使用）
- 标准库：`socket`、`sqlite3`、`json`、`uuid` 等

---

## 4. 系统架构

```text
┌─────────────────────────────────────────────────────────────┐
│                     AI Customer Service                     │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────┐  │
│  │  app.py      │  │ cli_chat.py  │  │ demo.py（连通性） │  │
│  │  Streamlit   │  │  终端多轮    │  │                  │  │
│  └──────┬───────┘  └──────┬───────┘  └────────┬─────────┘  │
│         └────────────┬────┴───────────────────┘            │
│                      ▼                                     │
│              service.py（编排）                             │
│         ┌────────────┼────────────┬────────────┐           │
│         ▼            ▼            ▼            ▼           │
│      rag.py       llm.py     prompt 缓存    store.py       │
│   MCP 召回     DashScope    get_project_   SQLite 会话     │
│                            prompt                         │
│         └────────────┬──────────────────────────────────┘  │
│                      ▼                                     │
│              mcp_client.py                                 │
│           Streamable HTTP …/mcp + Bearer                   │
└──────────────────────┬──────────────────────────────────────┘
                       │  search_documents / get_project_prompt …
                       ▼
┌─────────────────────────────────────────────────────────────┐
│              AI 数据中台（独立服务，需先启动）                 │
│  MCP Streamable HTTP → FastAPI → PostgreSQL + pgvector      │
│  文档入库 / 混合检索 / 项目 Prompt / 链路追踪                 │
└─────────────────────────────────────────────────────────────┘
```

**设计原则：**

- **关注点分离**：UI 不直接调 LLM/MCP，统一走 `CustomerService.ask()`；
- **协议边界清晰**：客服不持有 DB 连接，知识与权限由中台保证；
- **可独立演进**：换 UI（FastAPI + 前端）或换模型，只需改薄封装层。

---

## 5. 核心能力

### 5.1 知识库问答（RAG）

1. 用户提问 → 调用 MCP `search_documents`（可带 `project_id` / `top_k` / `threshold`）；
2. 解析返回文本为结构化片段（文件名、相似度、正文、召回来源标签）；
3. 拼装「知识库片段」上下文，交给 LLM，并附带系统提示词约束「只依据片段作答」。

### 5.2 项目级系统提示词

- 中台按 `project_id` 维护 `system` Prompt；
- 客服通过 MCP `get_project_prompt` 拉取，**本地缓存约 60 秒**；
- 拉取失败则回退到内置默认提示词，保证服务可用。

### 5.3 多轮会话与引用展示

- Streamlit：新建 / 切换历史会话，消息区展示回答、`trace_id`、知识库引用折叠面板；
- CLI：同一会话内维护 history，控制台打印引用摘要；
- SQLite：`sessions` + `messages`（`sources`、`trace_id` 字段便于复盘）。

### 5.4 降级策略

| 场景 | 行为 |
|------|------|
| 未配置 `DASHSCOPE_API_KEY` | 不调用 LLM，直接展示召回片段说明 |
| MCP 召回失败 | 返回明确错误文案，会话仍可继续 |
| LLM 调用失败 | 返回错误说明 + 原始召回上下文 |
| Prompt 拉取失败 | 使用本地默认 system prompt |

### 5.5 可观测性

- 召回时生成/透传 `trace_id`；
- 回答旁展示 `trace_id`，可在中台「链路追踪」页查看 MCP → API → 检索各 span。

---

## 6. 问答流水线

```text
用户问题
   │
   ├─① persist：写入 SQLite（user）
   │
   ├─② RagRetriever.retrieve
   │     · MCP tools/call → search_documents
   │     · 解析 chunks → 拼接 context
   │     · 记录 trace_id
   │
   ├─③ resolve_system_prompt（缓存）
   │     · MCP get_project_prompt 或默认文案
   │
   ├─④ DashScopeChat.generate
   │     · system + 近期 history + 知识库片段 + 用户问题
   │
   └─⑤ persist：写入 SQLite（assistant + sources + trace_id）
         · UI 渲染回答与引用
```

**防幻觉策略（提示词约束）：**

- 只依据【知识库片段】回答；
- 不足时明确说明「根据现有知识库无法确定」；
- 简洁分点、可引用文件名。

---

## 7. 目录与模块说明

```text
ai_customer/
├── README.md           # 本说明
├── .env.example        # 环境变量模板
├── .env                # 本地配置（勿提交密钥）
├── app.py              # Streamlit 客服主界面
├── cli_chat.py         # 终端多轮问答
├── demo.py             # MCP 连通性 / 工具自检
├── config.py           # Settings 加载
├── mcp_client.py       # MCP TCP 客户端封装
├── rag.py              # 召回与结果解析
├── llm.py              # DashScope 生成封装
├── service.py          # 业务编排（核心）
├── store.py            # SQLite 会话存储
└── data/
    ├── .gitkeep
    └── chat.db         # 运行后生成（默认路径）
```

| 模块 | 职责 | 面试可讲点 |
|------|------|------------|
| `mcp_client.py` | 一行一 JSON 的 TCP 客户端；错误码与 `trace_id` 解析 | 协议设计、超时与连接错误处理 |
| `rag.py` | 工具调用参数组装、文本结果正则解析、context 拼装 | RAG 中「检索结果结构化」 |
| `llm.py` | messages 组装、DashScope 调用、异常统一为 `LlmError` | 与模型厂商 SDK 解耦 |
| `service.py` | 唯一业务入口 `ask()`；Prompt 缓存；降级分支 | 编排层 / 防腐层 |
| `store.py` | 轻量会话库，无 ORM 依赖 | 何时用 SQLite 足够 |
| `app.py` | 状态管理、历史会话、引用 UI | Streamlit 会话态 |

---

## 8. 环境配置

复制模板并填写：

```bash
cp .env.example .env
```

| 变量 | 必填 | 说明 | 默认 |
|------|------|------|------|
| `PROJECT_ID` | 是 | 中台项目 UUID，限定检索范围 | 空 |
| `DASHSCOPE_API_KEY` | 问答生成时必填 | 通义 API Key；不填则仅召回降级 | 空 |
| `MCP_TRANSPORT` | 否 | `http`（默认）或遗留 `tcp` | `http` |
| `MCP_URL` | HTTP 时 | Streamable HTTP 地址 | `http://127.0.0.1:8765/mcp` |
| `MCP_CLIENT_TOKEN` | 视中台而定 | Bearer，与中台 `MCP_CLIENT_TOKEN` 一致 | 空 |
| `MCP_HOST` / `MCP_PORT` | TCP 时 | 遗留 TCP 地址（中台 `MCP_ENABLE_TCP`） | `127.0.0.1` / `8766` |
| `MCP_TCP_SECRET` | TCP 时 | 遗留 TCP auth_token | 空 |
| `MCP_TIMEOUT` | 否 | 超时（秒） | `120` |
| `LLM_MODEL` | 否 | 如 `qwen-plus` / `qwen-turbo` | `qwen-plus` |
| `TOP_K` | 否 | 召回条数 | `5` |
| `SEARCH_THRESHOLD` | 否 | 向量相似度阈值 | `0.45` |
| `SQLITE_PATH` | 否 | 相对本目录的库路径 | `data/chat.db` |

配置加载顺序（`config.py`）：

1. 本目录 `.env`
2. 若存在上两级仓库根 `.env`，**不覆盖**已有键（便于共用 `DASHSCOPE_API_KEY`）

---

## 9. 快速开始

### 9.1 前置：启动 AI 数据中台

在中台仓库中（与本目录同 monorepo 时，在仓库根执行）：

```bash
# 终端 A：API
uv run python start_api.py

# 终端 B：MCP
uv run python start_mcp.py
```

确认：

- 中台已配置 `MCP_API_KEY`，服务账号可访问目标项目；
- 目标 `PROJECT_ID` 下已有 **processed** 状态文档；
- （可选）在中台「提示词管理」为该项目配置 system Prompt。

### 9.2 配置本应用

```bash
cd models/ai_customer   # 或拷贝后的独立项目根目录
cp .env.example .env
# 编辑 PROJECT_ID、DASHSCOPE_API_KEY、可选 MCP_TCP_SECRET
```

### 9.3 启动客服

在**仓库根**（若仍在 monorepo）或本目录（需已安装依赖）：

```bash
# Web UI
uv run streamlit run models/ai_customer/app.py

# 终端多轮
uv run python models/ai_customer/cli_chat.py

# 连通性自检
uv run python models/ai_customer/demo.py --tool list
uv run python models/ai_customer/demo.py --tool search --query "请假流程"
```

浏览器打开 Streamlit 提示的本地地址（默认 `http://localhost:8501`），即可开始问答。

---

## 10. 运行模式说明

| 模式 | 命令 | 适用场景 |
|------|------|----------|
| Web 客服 | `streamlit run app.py` | 演示、产品原型、多会话管理 |
| CLI | `python cli_chat.py` | 快速调试、无浏览器环境 |
| Demo | `python demo.py --tool …` | 只验证 MCP 工具，不走完整 RAG 生成 |

`demo.py` 支持的工具示例：`list` / `search` / `stats` / `tables` 等，用于排查「客服无结果是检索问题还是生成问题」。

---

## 11. 与数据中台的协作边界

| 能力 | 归属 | 说明 |
|------|------|------|
| 文档上传、解析、向量化 | 中台 | 客服只读消费 |
| 混合检索、权限、租户隔离 | 中台 | 通过 `project_id` + MCP 鉴权 |
| 系统提示词 CRUD | 中台 UI/API | 客服只读拉取 |
| 链路追踪存储与查询 | 中台 | 客服只展示 `trace_id` |
| 对话 UI、会话历史 | **本项目** | 本地 SQLite |
| LLM 调用与降级策略 | **本项目** | DashScope |

这种拆分便于简历中强调：**「上层 AI 应用」与「企业知识中台」的分层架构**。

---

## 12. 扩展与二次开发建议

以下为合理演进方向（未全部实现，可作规划说明）：

1. **HTTP 直连中台**：用项目 `service_token` 调 `/api/search/hybrid`，适合无 MCP 的部署；
2. **流式输出**：DashScope stream + Streamlit `st.write_stream`；
3. **评价反馈**：点赞/点踩写回中台，用于检索与 Prompt 迭代；
4. **多 Agent**：工单创建、转人工，仍通过 MCP 工具扩展；
5. **独立仓库化**：增加本目录 `pyproject.toml` / `requirements.txt`，与中台完全分离发布。

---

## 13. 常见问题

| 现象 | 可能原因 | 处理 |
|------|----------|------|
| 无法连接 MCP | MCP 未启动 / 端口不对 | 确认 `start_mcp.py`，检查 `MCP_HOST`/`MCP_PORT` |
| 搜索无结果 | 项目 ID 错、文档未处理完、阈值过高 | 核对 `PROJECT_ID`；文档 `status=processed`；下调 `SEARCH_THRESHOLD` |
| 只召回不生成 | 未配 Key | 配置 `DASHSCOPE_API_KEY` |
| MCP 返回 403/鉴权失败 | 中台 API Key / 服务账号权限不足 | 检查中台 `MCP_API_KEY`、账号是否可访问项目 |
| 提示词不生效 | 缓存未过期 / 中台未保存 | 等待约 60s，或重启客服进程；确认中台已保存且为该 `project_id` |
| `trace_id` 查不到 | 中台追踪未开或未重启 | 确认中台 API 已升级并重启 |

---

## 14. 简历描述参考（可直接改写）

**项目名称：** 企业知识库智能客服（RAG + MCP）

**一句话：** 基于 MCP 协议消费企业知识中台的混合检索能力，结合通义千问实现可溯源、可降级的多轮智能客服，并支持按项目动态加载系统提示词。

**职责 / 成果（示例条目，按实际删改）：**

- 设计并实现 RAG 问答编排层：检索解析、上下文拼装、LLM 生成、引用与 `trace_id` 回传一体化；
- 实现 MCP TCP 客户端，解耦「客服应用」与「数据中台」，通过标准化工具调用完成检索与 Prompt 拉取；
- 使用 Streamlit 搭建多会话客服界面，SQLite 持久化历史消息与知识库引用；
- 落地降级策略（无 Key / 召回失败 / 生成失败）与 Prompt 短缓存，提升演示与联调稳定性；
- 对接中台混合检索与全链路追踪，支持问题排查与效果复盘。

**关键词：** Python · RAG · MCP · Streamlit · DashScope/Qwen · SQLite · 混合检索 · 可观测性 · 微服务解耦

---

## License / 说明

本应用作为 AI 数据中台的上层示例项目提供，便于学习 RAG 应用分层与 MCP 集成方式。  
若拆分为独立仓库对外展示，请自行补充 License，并**勿提交**含真实密钥的 `.env` 文件。
