# 智能客服 ↔ 工艺选型助手：如何通信

> 本文说明：`models/ai_customer` 如何与 `models/ai_quotation` 交换数据、关键代码在哪、以及**为什么用 HTTP 同步推荐 API，而不是再走一层 MCP / 把规则写进客服**。

相关代码：

| 侧 | 文件 | 作用 |
|----|------|------|
| 客服 | `tools.py` | 发起 HTTP、解析返回、stub 降级 |
| 客服 | `graph/nodes.py` | Tool Loop / 缺槽 Clarify / 再调工具 |
| 客服 | `prompts.py` | Agent 何时选 `process_config_recommend` |
| 客服 | `config.py` / `.env` | `PROCESS_CONFIG_URL` 等 |
| 工艺 | `api.py` | `POST /api/v1/recommend` |
| 工艺 | `service.py` → `recommend_for_customer` | 抽槽、校验五要素、出方案 |

---

## 1. 一句话结论

```text
客服只负责「问人、展示、留资」
工艺助手只负责「判五要素齐不齐、出配置方案」
二者用 HTTP JSON 同步通信；缺参时由工艺返回 missing，客服用 Clarify 追问后再 POST
```

**不**把五要素校验逻辑复制进客服；**不**让客服走工艺工作台那套人审 interrupt；**不**经中台 MCP 转发选型（文档/素材/大模型才走 MCP）。

---

## 2. 通信全景图

```text
用户（客服 Streamlit）
        │
        ▼
 LangGraph：agent_plan 选中工具 process_config_recommend
        │
        ▼
 CustomerTools.run(...)                         ← tools.py
        │
        │  POST http://127.0.0.1:8510/api/v1/recommend
        │  Body: { query, extras, slots, session_id }
        │  Header: Authorization: Bearer <可选>
        ▼
 ai_quotation/api.py  →  QuotationService.recommend_for_customer
        │
        ├─ 缺五要素 → { ok:false, error:"missing_required_slots", missing, clarify_question, slots_partial }
        │       │
        │       ▼
        │   客服 nodes.run_tools 发现 missing → need_slot_clarify
        │       → clarify（interrupt 问用户）
        │       → 用户补充后再次 run_tools → 再 POST（带上 slots_partial）
        │
        └─ 齐全 → { ok:true, proposal_text, structured, slots_partial }
                → judge 组织答复 →（可选）quote 留资写 CRM
```

和「文档检索」对比：

| 能力 | 协议 | 原因 |
|------|------|------|
| 搜文档 / 搜素材 / 调大模型 | **MCP** → 中台 | 知识与密钥在中台，多应用共享 |
| 工艺选型推荐 | **HTTP** → `ai_quotation:8510` | 选型是独立业务系统，有自己的规则/经验/人审工作台；客服只要同步「缺参或方案」 |

---

## 3. 配置：客服怎么知道调谁

`models/ai_customer/.env`：

```text
PROCESS_CONFIG_URL=http://127.0.0.1:8510
# PROCESS_CONFIG_TOKEN=   # 与工艺侧 CUSTOMER_API_TOKEN 一致（若启用）
PROCESS_CONFIG_TIMEOUT=120
```

对应读取（`config.py`）：

```python
process_config_url: str = env("PROCESS_CONFIG_URL")
process_config_token: str = env("PROCESS_CONFIG_TOKEN")
process_config_timeout: float = float(env("PROCESS_CONFIG_TIMEOUT", "120") or "120")
```

工艺侧启动推荐服务：

```bash
cd models/ai_quotation
uv run python api.py   # 默认监听 :8510
```

未配置 `PROCESS_CONFIG_URL` 时，客服走 **stub**（本地模拟文案），方便单独演示客服图，不强制起工艺进程。

---

## 4. 客服侧：具体怎么发请求

### 4.1 工具入口

Agent 规划出的工具名是 `process_config_recommend`。  
真正执行在 `CustomerTools.run`：

```274:284:models/ai_customer/tools.py
        if name == "process_config_recommend":
            if settings.process_config_url:
                return process_config_recommend_live(
                    query,
                    extras=extras,
                    slots=slots,
                    session_id=session_id,
                )
            return process_config_recommend_stub(
                query, extras=extras, slots=slots
            )
```

**设计点**：图节点只认工具名；换 stub / 真 HTTP / 将来换实现，**不必改 LangGraph 拓扑**。

### 4.2 拼 URL 并发 POST

```58:103:models/ai_customer/tools.py
def process_config_recommend_live(
    query: str,
    *,
    extras: str = "",
    slots: dict[str, Any] | None = None,
    session_id: str = "",
) -> dict[str, Any]:
    """
    调用 ai_quotation POST /api/v1/recommend。

    缺五要素时返回 ok=False + missing，由客服 Clarify Loop 追问。
    """
    url = (settings.process_config_url or "").rstrip("/")
    if not url.endswith("/recommend"):
        # 允许只配到 host:port 或 /api/v1
        if url.endswith("/api/v1"):
            url = url + "/recommend"
        else:
            url = url + "/api/v1/recommend"

    headers = {"Content-Type": "application/json"}
    token = (settings.process_config_token or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    payload = {
        "query": query or "",
        "extras": extras or "",
        "slots": slots or {},
        "session_id": session_id or "",
    }
    try:
        with httpx.Client(timeout=settings.process_config_timeout) as client:
            resp = client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        ...
```

请求体字段含义：

| 字段 | 谁填 | 含义 |
|------|------|------|
| `query` | 客服 Agent 的 `tool_query` 或用户原话 | 本轮检索/选型短句 |
| `extras` | Clarify 累积的用户补充 | 多轮追问拼起来的说明 |
| `slots` | 状态里的 `process_slots` | 已识别槽位，多轮回传避免丢字段 |
| `session_id` | 客服会话 ID | 便于两边日志对齐（可选） |

### 4.3 把工艺返回「翻译」成图节点能用的统一结构

缺五要素时：

```122:138:models/ai_customer/tools.py
    if not ok and data.get("error") == "missing_required_slots":
        labels = missing_labels or missing
        text = clarify_q or (
            "生成配置清单前还需确认：" + "、".join(str(x) for x in labels) + "。"
        )
        return {
            "tool": "process_config_recommend",
            "ok": False,
            "text": text,
            "error": "missing_required_slots",
            "missing": missing,
            "missing_labels": missing_labels,
            "clarify_question": text,
            "slots_partial": slots_partial,
            ...
        }
```

齐全时：把 `proposal_text` 放进 `text`，供 judge / 最终答复使用。

**设计点**：节点层只看 `ok` / `error` / `missing` / `text`，不直接依赖工艺内部类名，降低耦合。

---

## 5. 客服图：缺参时如何追问再重试

`run_tools` 里若发现工艺返回 `missing_required_slots`：

```486:508:models/ai_customer/graph/nodes.py
            if (
                name == "process_config_recommend"
                and tr.get("error") == "missing_required_slots"
            ):
                need_slot_clarify = True
                ...
                clarify_plan = {
                    **clarify_plan,
                    "tools": ["process_config_recommend"],
                    "info_enough": False,
                    "missing_info": list(labels),
                    "clarify_question": ask,
                    "tool_query": query,
                }
```

路由：

```540:543:models/ai_customer/graph/nodes.py
    def route_after_tools(state: CustomerState) -> str:
        if state.get("need_slot_clarify"):
            return "clarify"
        return "judge"
```

用户补完后，`clarify` 节点会把 `need_slot_clarify` 清掉，并强制下一步再跑 `process_config_recommend`（避免再绕一圈 Agent 乱选工具）：

```412:420:models/ai_customer/graph/nodes.py
        if state.get("need_slot_clarify"):
            plan = dict(state.get("plan") or {})
            plan["info_enough"] = True
            if "process_config_recommend" not in (plan.get("tools") or []):
                plan["tools"] = ["process_config_recommend"]
            out["plan"] = plan
            out["need_slot_clarify"] = False
            out["status"] = "running"
```

同时 `process_slots` 会合并工具返回的 `slots_partial`，下次 POST 带上，减少「同一字段问两遍」。

---

## 6. 工艺侧：接口与业务真相源

### 6.1 HTTP 入口

```72:87:models/ai_quotation/api.py
@app.post("/api/v1/recommend")
def recommend(
    body: RecommendRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _check_auth(authorization)
    svc = get_svc()
    result = svc.recommend_for_customer(
        query=body.query,
        extras=body.extras,
        slots=body.slots or None,
    )
    if body.session_id:
        result = {**result, "session_id": body.session_id}
    return result
```

注意：`QuotationService(start_harness_loop=False)` —— **客服 API 进程不启后台人审扫描**，避免与工程师 Streamlit 工作台双开冲突。

### 6.2 同步推荐（刻意跳过人审 interrupt）

```153:186:models/ai_quotation/service.py
    def recommend_for_customer(...):
        """
        供智能客服同步调用：抽槽 → 校验五要素 → 生成配置方案正文。

        不走 LangGraph 人审 interrupt；缺参时返回 missing，由客服 Clarify 追问。
        """
        ...
        missing = merged.missing_required()
        ...
        if missing:
            return {
                "ok": False,
                "error": "missing_required_slots",
                "missing": missing,
                "missing_labels": labels,
                "clarify_question": merged.clarify_prompt(),
                "slots_partial": slots_partial,
                ...
            }
```

齐全后才会：可选知识库检索 → `build_configuration` → 可选 LLM 润色 → 套用选型经验 → 返回 `proposal_text`。

**五要素是否齐，以工艺侧 `missing_required()` 为准**，客服不维护第二套校验表。

---

## 7. 为什么要这样做（设计取舍）

### 7.1 为什么用 HTTP，而不是 MCP？

| 方案 | 优点 | 缺点 / 为何没选 |
|------|------|-----------------|
| **HTTP 直连工艺 API（现行）** | 契约简单、同步阻塞适合 Tool Loop；工艺可独立扩缩容/拆仓；与中台解耦 | 客服要多配一个 URL |
| 再包一层 MCP 工具 | 和搜文档「看起来一致」 | 多一跳延迟与运维；选型不是中台通用知识能力，硬塞进中台 MCP 会污染边界 |
| 客服进程内 import 工艺代码 | 本地快 | 部署耦合、无法独立发布工艺规则 |

MCP 适合「中台统一能力」（检索、LLM、素材）。  
工艺选型是**带业务规则与经验库的独立产品**，用专用 HTTP 更清晰。

### 7.2 为什么缺参由工艺返回，而不是客服自己先问齐？

- **单一真相源**：细度 vs d95、通筛率等口径只在工艺侧维护一份。  
- 客服 Agent 可以「先试探调用」：工艺说缺什么，再精准追问，避免客服瞎问一堆无关项。  
- 工程师改五要素规则时，**不必改客服提示词里的硬编码列表**（提示词只给 Agent 方向性说明）。

### 7.3 为什么客服捷径不走人审 interrupt？

工程师工作台需要：生成 → **暂停等人审** → 改判 → 写经验。  
客服对话需要：用户等几秒内拿到「当前可展示方案」或「还缺哪几项」。

若客服也卡在人审 interrupt：

- HTTP 无法同步返回  
- 用户会话会挂起等内部工程师，产品形态不对  

所以：`recommend_for_customer` = **同步捷径**；人审与经验学习留在 `:8504` 工作台。

### 7.4 为什么还要 stub？

本地只跑客服、演示 Agent/Clarify 拓扑时，可以不启工艺进程。  
节点与工具名不变，接上 `PROCESS_CONFIG_URL` 即切真链路 —— **契约稳定、实现可替换**。

### 7.5 和「报价留资」的边界

工艺返回的是 **配置方案文案**，不是商务成交价。  
用户说要报价时，由客服 `quote_offer` / `quote_collect` 收姓名与联系方式，写入 `crm_leads`。  
**联系销售 / CRM 不经过工艺 API。**

---

## 8. 联调最小步骤

1. 中台 API + MCP 可选（工艺若要用检索/LLM 则需要）  
2. `cd models/ai_quotation && uv run python api.py`  
3. 客服 `.env` 设 `PROCESS_CONFIG_URL=http://127.0.0.1:8510`  
4. 客服提问含选型意图的话，例如物料+细度+产量…  
5. 故意少说一项 → 应出现 Clarify 追问 → 补全后再出方案  

健康检查：`GET http://127.0.0.1:8510/health`

---

## 9. 相关阅读

- 客服总览：[README.md](./README.md)  
- 客服流程深挖：[FLOW_ANALYSIS.md](./FLOW_ANALYSIS.md)  
- 工艺选型总览：[../ai_quotation/README.md](../ai_quotation/README.md)  
