# 架构设计文档

## 整体架构

```
┌─────────────────────────────────────────────────────────────────┐
│                          GitCode                                │
│   MR 创建/更新事件              评论区命令输入                    │
└──────────┬──────────────────────────┬───────────────────────────┘
           │ Webhook                  │ Webhook (Note 事件)
           ▼                          ▼
┌─────────────────────────────────────────────────────────────────┐
│                  FastAPI Webhook Gateway                         │
│                                                                 │
│  EventRouter                                                    │
│    ├── merge_request (opened/updated) ──▶ ReviewOrchestrator    │
│    ├── push  ──▶ SuggestionApplyHandler                         │
│    │             └── 匹配 commit msg "Apply.*suggestion"        │
│    │                   → 更新 suggestion_status                 │
│    │                   → 重算风险等级，更新 MR label             │
│    └── note  ──▶ CommandParser                                  │
│                    ├── /ai review  ──▶ ReviewOrchestrator       │
│                    ├── /ai summary ──▶ SummaryAgent (单独触发)  │
│                    ├── /ai explain ──▶ ExplainAgent             │
│                    └── /ai help    ──▶ 直接回复命令列表          │
└──────────────────────────┬──────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│                LangGraph ReviewOrchestrator                      │
│                                                                 │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │               Supervisor 动态循环（Multi-Agent 核心）      │   │
│  │                                                          │   │
│  │  ┌───────────────────────────────────────────────────┐  │   │
│  │  │  Supervisor Agent（每轮完整 LLM 推理）              │  │   │
│  │  │  读取：完整 State 含历史 findings + 已执行轮次      │◀─┐│   │
│  │  │  输出：SupervisorDecision（DISPATCH / FINISH）     │  ││   │
│  │  └────────────────────┬──────────────────────────────┘  ││   │
│  │                       │                                  ││   │
│  │           ┌───────────┴───────────┐                     ││   │
│  │      DISPATCH                  FINISH                   ││   │
│  │           ▼                       ▼                     ││   │
│  │  按本轮决策并行召唤 Agent      退出循环                   ││   │
│  │  ┌──────────┬──────────┬──────────────┐                 ││   │
│  │  ▼          ▼          ▼              ▼                  ││   │
│  │ Security  Logic     Quality      Performance             ││   │
│  │  Agent    Agent      Agent         Agent                 ││   │
│  │ (ReAct)  (ReAct)   (ReAct)       (ReAct)                ││   │
│  │  自主决定  含focus_  含RAG查询     自主决定               ││   │
│  │  查哪些文件 hint上下文 团队规范     查哪些文件             ││   │
│  │  ┌──────────┴──────────┴──────────────┘                 ││   │
│  │  │  findings 汇入 State（operator.add 自动聚合）          │┘   │
│  │  └──────────────────────────────────────────────────────┘    │
│  └──────────────────────────────────────────────────────────┘   │
│                       │ FINISH                                   │
│                       ▼                                         │
│              ┌─────────────────┐                                │
│              │  Summary Agent  │  单次 LLM 调用                  │
│              └────────┬────────┘                                │
│                       ▼                                         │
│             synthesize_node  去重 + severity 排序                │
│                       ▼                                         │
│               critic_node   建议质量评估（Reflection）            │
│                       ▼                                         │
│              publish_node   统一写回 GitCode                     │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│          GitCode MCP Server（Streamable HTTP 协议）               │
│                                                                 │
│  读取工具（专家 Agent ReAct 循环调用）                            │
│  ├── get_pr_diff(project_id, mr_iid)                            │
│  ├── get_file_content(project_id, path, ref)                    │
│  └── search_team_norms(query)                                   │
│                                                                 │
│  写入工具（仅 publish_node 调用）                                 │
│  ├── post_inline_comment(project_id, mr_iid, path, line, body)  │
│  ├── post_suggestion(project_id, mr_iid, path, line, code)      │
│  ├── update_mr_description(project_id, mr_iid, body)            │
│  └── update_mr_label(project_id, mr_iid, labels)               │
└─────────────────────────────────────────────────────────────────┘

┌──────────────────┐  ┌──────────────┐  ┌──────────────────────┐
│      MySQL       │  │    Redis     │  │   Elasticsearch      │
│                  │  │              │  │                      │
│  review_tasks    │  │ 幂等去重锁    │  │ 团队规范向量索引       │
│  review_results  │  │ 进行中状态   │  │ 历史 PR 知识库        │
│  suggestion_     │  │              │  │ （混合检索 RAG）      │
│    status        │  │              │  │                      │
└──────────────────┘  └──────────────┘  └──────────────────────┘
```

---

## JSON Schema 规范

> Agent 间所有交互、Agent 内部每步输入输出均使用以下 Schema，禁止纯文字传递。

### Supervisor 输出 Schema（每轮动态决策）

> Supervisor 每轮读取完整 State（含历史 findings），输出本轮决策。不是一次性全量计划。

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "SupervisorDecision",
  "type": "object",
  "required": ["action", "reasoning", "agents_to_dispatch"],
  "properties": {
    "action": {
      "type": "string",
      "enum": ["DISPATCH", "FINISH"],
      "description": "DISPATCH：继续派遣 Agent；FINISH：调查充分，进入 SummaryAgent"
    },
    "reasoning": {
      "type": "string",
      "description": "本轮决策依据，基于当前 findings 的推理过程，记录在 State 中供审计"
    },
    "agents_to_dispatch": {
      "type": "array",
      "description": "本轮并行召唤的 Agent 任务，action=FINISH 时为空",
      "items": {
        "type": "object",
        "required": ["agent_type", "files"],
        "properties": {
          "agent_type": {
            "type": "string",
            "enum": ["SecurityAgent", "LogicAgent", "QualityAgent", "PerformanceAgent"]
          },
          "files": {
            "type": "array",
            "items": { "type": "string" },
            "description": "本轮分配给该 Agent 的文件列表"
          },
          "focus_hint": {
            "type": "string",
            "description": "基于上一轮 findings 的专项提示，如'追查 auth/login.py 第45行的数据流'，首轮为空"
          }
        }
      }
    },
    "pr_meta": {
      "type": "object",
      "description": "仅首轮输出，后续轮次复用",
      "properties": {
        "total_files":       { "type": "integer" },
        "total_lines":       { "type": "integer" },
        "sensitive_modules": { "type": "array", "items": { "type": "string" } }
      }
    }
  }
}
```

### 专家 Agent 任务输入 Schema（Supervisor → 各 Agent）

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "AgentTask",
  "type": "object",
  "required": ["task_id", "agent_type", "project_id", "mr_iid", "file_chunk", "diff_slice"],
  "properties": {
    "task_id":     { "type": "string", "description": "全局唯一任务 ID" },
    "agent_type":  { "type": "string", "enum": ["SecurityAgent","LogicAgent","QualityAgent","PerformanceAgent"] },
    "project_id":  { "type": "integer" },
    "mr_iid":      { "type": "integer" },
    "file_chunk":  { "type": "array", "items": { "type": "string" }, "description": "本 Agent 负责的文件列表" },
    "diff_slice":  { "type": "string", "description": "对应文件的 diff 片段" }
  }
}
```

### 专家 Agent Finding Schema（各 Agent → State）

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "Finding",
  "type": "object",
  "required": ["finding_id", "agent", "severity", "category", "file", "line_start", "description", "suggestion_code"],
  "properties": {
    "finding_id":       { "type": "string", "description": "UUID，用于 suggestion_status 追踪" },
    "agent":            { "type": "string", "enum": ["SecurityAgent","LogicAgent","QualityAgent","PerformanceAgent"] },
    "severity":         { "type": "string", "enum": ["CRITICAL","HIGH","MEDIUM","LOW"] },
    "category": {
      "type": "string",
      "enum": ["security", "logic", "quality", "performance"]
    },
    "file":             { "type": "string" },
    "line_start":       { "type": "integer" },
    "line_end":         { "type": "integer" },
    "description":      { "type": "string", "description": "问题描述，面向开发者" },
    "suggestion_code":  { "type": "string", "description": "建议替换的代码内容，用于 suggestion block" },
    "norm_reference":   { "type": "string", "description": "引用的团队规范片段（QualityAgent 专用）" }
  }
}
```

### SummaryAgent 输入 Schema（State → SummaryAgent）

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "SummaryInput",
  "type": "object",
  "required": ["pr_meta", "all_findings"],
  "properties": {
    "pr_meta":       { "$ref": "#/definitions/PrMeta" },
    "all_findings": {
      "type": "array",
      "items": { "$ref": "Finding" }
    }
  },
  "definitions": {
    "PrMeta": {
      "type": "object",
      "properties": {
        "total_files":       { "type": "integer" },
        "total_lines":       { "type": "integer" },
        "sensitive_modules": { "type": "array", "items": { "type": "string" } },
        "risk_hint":         { "type": "string" }
      }
    }
  }
}
```

### SummaryAgent 输出 Schema

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "SummaryOutput",
  "type": "object",
  "required": ["total_files", "total_lines", "impact_analysis", "risk_level", "focus_points"],
  "properties": {
    "total_files":      { "type": "integer" },
    "total_lines":      { "type": "integer" },
    "impact_analysis":  { "type": "string", "description": "改动范围与影响面分析" },
    "risk_level": {
      "type": "string",
      "enum": ["LOW", "MEDIUM", "HIGH", "CRITICAL"]
    },
    "risk_reason":   { "type": "string", "description": "风险等级主因说明" },
    "focus_points": {
      "type": "array",
      "items": { "type": "string" },
      "description": "主要关注点提示，最多 5 条"
    }
  }
}
```

### ExplainAgent 输入 / 输出 Schema

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "ExplainRequest",
  "type": "object",
  "required": ["project_id", "mr_iid", "file_path", "line_number", "parent_comment"],
  "properties": {
    "project_id":      { "type": "integer" },
    "mr_iid":          { "type": "integer" },
    "file_path":       { "type": "string" },
    "line_number":     { "type": "integer" },
    "parent_comment":  { "type": "string", "description": "触发 /ai explain 的父 comment 内容" },
    "code_context":    { "type": "string", "description": "文件前后 20 行上下文" },
    "norm_context":    { "type": "string", "description": "ES 查询到的相关团队规范" }
  }
}
```

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "ExplainResponse",
  "type": "object",
  "required": ["explanation", "reply_to_comment_id"],
  "properties": {
    "explanation":          { "type": "string" },
    "reply_to_comment_id":  { "type": "integer" }
  }
}
```

---

## LangGraph State 定义

```python
from typing import TypedDict, Annotated
import operator

class Finding(TypedDict):
    finding_id:      str   # UUID
    agent:           str   # SecurityAgent / LogicAgent / ...
    severity:        str   # CRITICAL / HIGH / MEDIUM / LOW
    category:        str   # security / logic / quality / performance
    file:            str
    line_start:      int
    line_end:        int
    description:     str
    suggestion_code: str
    norm_reference:  str   # QualityAgent 引用的团队规范片段

class SummaryOutput(TypedDict):
    total_files:     int
    total_lines:     int
    impact_analysis: str
    risk_level:      str   # LOW / MEDIUM / HIGH / CRITICAL
    risk_reason:     str
    focus_points:    list[str]

class ReviewState(TypedDict):
    # 输入
    project_id:  int
    mr_iid:      int
    commit_sha:  str
    raw_diff:    str
    file_list:   list[str]

    # Supervisor 动态循环控制
    iteration:            int        # 当前轮次，防止无限循环（max=5）
    supervisor_reasoning: Annotated[list[str], operator.add]  # 每轮决策推理，追加记录
    pr_meta:              dict       # 首轮填入，后续轮次只读

    # 专家 Agent 输出（跨轮次、跨 Agent 自动聚合）
    findings: Annotated[list[Finding], operator.add]

    # Summary / 最终
    summary:        SummaryOutput
    final_findings: list[Finding]  # synthesize_node 去重排序后
```

---

## 大 PR 处理：Map-Reduce 策略

纯 ReAct 在大 PR（50+ 文件，1000+ 行 diff）下会导致 context 爆炸，采用 Map-Reduce：

```
整个 diff
    ↓
Supervisor 按文件分块（每块 ≤ 10 文件）
    ↓
每块 × 每个选中 Agent → 并行 ReAct（每个 ReAct session context 可控）
[块1×Security] [块1×Logic] [块2×Security] [块2×Logic] ...
    ↓
所有 findings 汇入 State（operator.add 自动聚合）
    ↓
SummaryAgent 单次 LLM 调用（接收聚合后的 findings，不做 ReAct）
    ↓
synthesize_node 去重 + 排序
    ↓
publish_node 统一写回
```

**分块规则（Supervisor 执行）：**
- 默认每块 ≤ 10 个文件
- 敏感文件（auth / payment / db 路径）单独成块，优先处理
- 纯文档 / 注释变更文件跳过专家 Agent，直接进 SummaryAgent

**ReAct 轮次限制：** 每个专家 Agent session `max_iterations = 8`，超出强制结束并标记 `truncated: true`。

**Agent 局部失败处理：**

```
某个专家 Agent ReAct session 抛出异常或超时
  → 捕获异常，该 Agent 输出空 findings（不中断整体流程）
  → 在 SummaryAgent 的 focus_points 中追加警告：
    "SecurityAgent 检视异常，建议人工复查安全相关变更"
  → MySQL review_results 记录该 Agent 的 status = 'failed'
  → 整体 review_tasks.status 仍为 'done'（部分完成）
```

**规则：单个 Agent 失败不影响其他 Agent 和最终发布，但在摘要中透明告知用户。**

---

## 核心模块说明

### 1. Webhook Gateway（`src/webhook/main.py`）

**端点：**
- `POST /webhook` — 接收所有 GitCode Webhook 事件（需验证 Secret Token）
- `GET /health` — 健康检查

**Webhook Secret 验证：**
```python
# 验证 X-Gitlab-Token header，防止伪造请求
if request.headers.get("X-Gitlab-Token") != settings.WEBHOOK_SECRET:
    raise HTTPException(status_code=401)
```

**幂等保障：**

| 触发来源 | Redis Key | 行为 |
|---------|-----------|------|
| MR 自动触发 | `review:{project_id}:{mr_iid}:{commit_sha}` | 存在则跳过，防止同一 commit 重复检视 |
| `/ai review` 命令 | `review:{project_id}:{mr_iid}:cmd:{timestamp}` | 每次命令生成新 key，**始终执行**，不受自动触发的锁影响 |
| `/ai summary` 命令 | 不加锁 | SummaryAgent 单次调用，幂等本身开销低 |

---

### 2. Supervisor Agent（`src/agents/supervisor.py`）

**模式：** 动态循环，每轮 LLM 推理（非 ReAct，无工具调用，但多轮执行）

**输入（每轮）：** 完整 ReviewState，包含：
- 首轮：raw_diff + file_list（无历史 findings）
- 后续轮：raw_diff + file_list + 历史 findings + supervisor_reasoning

**输出（每轮）：** `SupervisorDecision`（JSON Schema 见上）

**循环终止条件（满足任一即输出 FINISH）：**
- LLM 判断当前 findings 已覆盖所有文件和风险点
- `iteration >= 5`（硬性上限，防止无限循环）
- 本轮 `agents_to_dispatch` 为空（无新任务）

**决策示例（体现 Multi-Agent 动态性）：**
```
第1轮：分析 diff，发现涉及 auth + db，
       → DISPATCH [SecurityAgent(全部文件), LogicAgent(全部文件),
                   QualityAgent(全部文件), PerformanceAgent(全部文件)]
第2轮：读取 findings，SecurityAgent 发现 auth/login.py:45 有 SQL 注入，
       → DISPATCH [LogicAgent(files=["auth/login.py"],
                              focus_hint="追查第45行参数传递路径")]
第3轮：追查 findings 充分，无新风险点，
       → FINISH
```

---

### 3. 专家 Agent 设计

所有专家 Agent 使用 ReAct 模式，**只调用读取工具，不写 GitCode**。

| Agent | 文件 | 检测范围 | 可用工具 |
|-------|------|---------|---------|
| SecurityAgent | `security_agent.py` | SQL注入、硬编码密钥、明文密码、不安全随机数 | `get_file_content` |
| LogicAgent | `logic_agent.py` | 空指针、资源泄露、竞态条件、整数溢出 | `get_file_content` |
| QualityAgent | `quality_agent.py` | 过长函数、重复代码、命名不规范 | `get_file_content`, `search_team_norms` |
| PerformanceAgent | `performance_agent.py` | N+1 查询、不必要循环、内存浪费 | `get_file_content` |

**每个 Agent 输出：** `list[Finding]`（JSON Schema 见上）

---

### 4. SummaryAgent（`src/agents/summary_agent.py`）

**模式：** 单次 LLM 调用（不使用 ReAct，不调工具）

**输入：** `SummaryInput`（所有 findings + pr_meta）
**输出：** `SummaryOutput`

**风险评级权重：**

| 维度 | 权重 | 高风险信号 |
|------|------|-----------|
| 安全 / 逻辑发现 | 40% | CRITICAL / HIGH finding 数量 |
| 改动范围 | 30% | 文件数 > 10 或核心模块变更 |
| 测试覆盖 | 20% | 改动函数无对应测试新增 |
| 历史热点 | 10% | Supervisor 标记的敏感模块 |

---

### 5. ExplainAgent（`src/agents/explain_agent.py`）

**模式：** 轻量 ReAct（固定 2 步：get_file_content → search_team_norms → 生成解释）

**触发：** 用户在任意 inline comment 下回复 `/ai explain`

**输入来源：**
- `note.position.new_path` → file_path
- `note.position.new_line` → line_number
- 父 comment body → parent_comment

---

### 6. synthesize_node

对 `state.findings` 执行：
1. **去重**：按 `(file, line_start, category)` 组合键去重，不用 `finding_id`（同一问题在不同 chunk 中会产生不同 UUID，但文件 + 行号 + 类别相同）；保留 severity 最高的那条
2. **排序**：CRITICAL → HIGH → MEDIUM → LOW
3. 写入 `state.final_findings`

---

### 7. publish_node

遍历 `state.final_findings`，调用 MCP 写入工具：
- 每条 finding → `post_inline_comment`（含问题描述）
- finding 有 `suggestion_code` → 追加 `post_suggestion`
- 写入 `suggestion_status` 表（记录 comment_id + finding_id + severity）
- 完成后调用 `update_mr_description`（写入 summary）
- 调用 `update_mr_label`（写入风险等级 label）

---

### 8. GitCode MCP Server（`src/mcp/gitcode_server.py`）

封装 GitCode（GitLab v4 兼容）REST API，暴露为 MCP 工具。

**传输协议：Streamable HTTP**（MCP 2025 规范推荐，双向流，替代旧版 SSE）
- MCP Server 作为独立 FastAPI 应用运行，端口 8081
- Agent 通过 `POST /mcp` 端点发起工具调用请求
- 相比 SSE 支持双向通信，适合生产级部署

**Suggestion Block 格式：**
````
```suggestion
替换后的代码内容
```
````

**MCP 工具 Input/Output Schema：**

```json
// get_pr_diff
{
  "input":  { "project_id": "integer", "mr_iid": "integer" },
  "output": {
    "diff": "string",
    "base_sha": "string",
    "head_sha": "string",
    "start_sha": "string"
  }
}

// get_file_content
{
  "input":  { "project_id": "integer", "file_path": "string", "ref": "string" },
  "output": { "content": "string" }
}

// search_team_norms
{
  "input":  { "query": "string", "top_k": "integer (default: 3)" },
  "output": {
    "results": [
      { "content": "string", "source": "string", "score": "number" }
    ]
  }
}

// post_inline_comment（GitLab position 对象必传）
{
  "input": {
    "project_id": "integer",
    "mr_iid":     "integer",
    "body":       "string",
    "position": {
      "base_sha":       "string  ← 来自 get_pr_diff.base_sha",
      "start_sha":      "string  ← 来自 get_pr_diff.start_sha",
      "head_sha":       "string  ← 来自 get_pr_diff.head_sha",
      "position_type":  "text",
      "new_path":       "string  ← file_path",
      "new_line":       "integer ← Finding.line_start"
    }
  },
  "output": { "comment_id": "integer" }
}

// post_suggestion（body 内嵌 suggestion block）
{
  "input": {
    "project_id":      "integer",
    "mr_iid":          "integer",
    "suggestion_code": "string",
    "position":        "同 post_inline_comment.position"
  },
  "output": { "comment_id": "integer" }
}

// update_mr_description
{
  "input":  { "project_id": "integer", "mr_iid": "integer", "body": "string" },
  "output": { "success": "boolean" }
}

// update_mr_label
{
  "input":  { "project_id": "integer", "mr_iid": "integer", "labels": ["string"] },
  "output": { "success": "boolean" }
}
```

> **说明：** `post_inline_comment` 的 `position` 对象中 `base_sha / start_sha / head_sha` 必须来自当次 `get_pr_diff` 的返回值，publish_node 负责在调用写入工具前先调一次 `get_pr_diff` 拿到这三个值。

---

### 9. RAG 知识库（Elasticsearch）

**索引结构：**
- `team-norms` 索引：团队 Coding Guideline 文档切片（稠密向量 + BM25 混合检索）
- `pr-history` 索引：历史已合并高质量 PR diff 片段（后续扩展）

**`search_team_norms(query)` 实现：** ES 混合检索，返回 top-3 相关父块内容，拼入 QualityAgent 的 prompt context。

---

### 10. 知识库初始化（`src/tools/ingest_norms.py`）

**触发方式：** 命令行工具，文件路径作为参数

```bash
python -m src.tools.ingest_norms --path ./docs/coding_standards.md
python -m src.tools.ingest_norms --path ./docs/java_guidelines.pdf
```

**分块策略：父子分块（Parent-Child Chunking）**

参考 know-engine 实现，父子分块在检索精度和上下文完整性之间取得平衡：

```
原始文档
    ↓ 切分
父块（Parent Chunk）：段落级，500~1000 tokens
  ├── 子块1（Child Chunk）：句子级，100~200 tokens  ← 向量化、用于检索
  ├── 子块2（Child Chunk）
  └── 子块3（Child Chunk）

检索时：
  用户 query → 向量搜索匹配子块 → 返回子块对应的父块内容
  （子块精准定位，父块保证上下文完整）
```

**处理流程：**

```
1. 读取文件（支持 .md / .txt / .pdf）
2. 按段落切分为父块（ParentTextSplitter，chunk_size=800）
3. 每个父块再切分为子块（ChildTextSplitter，chunk_size=150，overlap=20）
4. 子块向量化（DashScope Embedding）
5. ES 写入：
   - parent_doc 索引：存储父块原文 + metadata（source, section）
   - team-norms 索引：存储子块向量 + parent_id 外键
6. 输出入库统计（父块数 / 子块数 / 耗时）
```

**ES 索引 Mapping（team-norms）：**

```json
{
  "mappings": {
    "properties": {
      "parent_id":  { "type": "keyword" },
      "content":    { "type": "text", "analyzer": "ik_max_word" },
      "embedding":  { "type": "dense_vector", "dims": 1536 },
      "source":     { "type": "keyword" },
      "chunk_index":{ "type": "integer" }
    }
  }
}
```

---

## 数据库设计

### review_tasks

```sql
CREATE TABLE review_tasks (
    id           BIGINT PRIMARY KEY AUTO_INCREMENT,
    project_id   BIGINT      NOT NULL,
    mr_iid       INT         NOT NULL,
    commit_sha   VARCHAR(40) NOT NULL,
    status       ENUM('pending','running','done','failed') DEFAULT 'pending',
    triggered_by VARCHAR(20) COMMENT 'auto / command',
    created_at   DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at   DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_project_mr_commit (project_id, mr_iid, commit_sha),
    INDEX idx_status (status)
);
```

### review_results

```sql
CREATE TABLE review_results (
    id         BIGINT PRIMARY KEY AUTO_INCREMENT,
    task_id    BIGINT      NOT NULL,
    agent      VARCHAR(50) NOT NULL COMMENT 'SecurityAgent / LogicAgent / ...',
    status     ENUM('done','failed') DEFAULT 'done',
    findings   JSON        COMMENT '符合 Finding Schema 的数组，failed 时为 null',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_task (task_id)
);
```

### suggestion_status

```sql
CREATE TABLE suggestion_status (
    id          BIGINT PRIMARY KEY AUTO_INCREMENT,
    task_id     BIGINT       NOT NULL,
    finding_id  VARCHAR(36)  NOT NULL COMMENT 'Finding.finding_id UUID',
    comment_id  BIGINT       NOT NULL COMMENT 'GitCode 写回的 comment ID',
    file_path   VARCHAR(500) NOT NULL,
    line_number INT          NOT NULL,
    severity    ENUM('CRITICAL','HIGH','MEDIUM','LOW') NOT NULL,
    status      ENUM('open','applied','dismissed') DEFAULT 'open',
    updated_at  DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_task_file (task_id, file_path),
    INDEX idx_finding  (finding_id)
);
```

**风险等级 label 重算逻辑（suggestion apply 后触发）：**

```
查询 task_id 关联的所有 suggestion_status
  → 统计 severity IN ('CRITICAL','HIGH') AND status = 'open' 的数量 N
  → N == 0 : update_mr_label(['ai-risk:low'])
  → N  > 0 : update_mr_label(['ai-risk:high'])
```

---

## 关键流程

### MR 自动检视流程

```
1.  GitCode 推送 merge_request Webhook
2.  验证 X-Gitlab-Token
3.  Redis 幂等检查：key review:{project_id}:{mr_iid}:{commit_sha} 存在 → 跳过
4.  MySQL 写入 review_tasks（status=pending）
5.  启动 LangGraph ReviewOrchestrator（异步，FastAPI 立即返回 202）
6.  supervisor_node：单次 LLM 调用，输出 SupervisorDecision（JSON）
7.  Map 阶段：按 file_chunks × selected_agents 展开并行任务
    每个 Agent session 独立 ReAct，max_iterations=8
    输出 list[Finding]，通过 operator.add 聚合到 state.findings
8.  summary_node：单次 LLM 调用，输出 SummaryOutput（JSON）
9.  synthesize_node：去重 + severity 排序 → state.final_findings
10. publish_node：遍历 final_findings，调 MCP 写入工具
    - post_inline_comment（每条 finding）
    - post_suggestion（有 suggestion_code 的 finding）
    - 写 suggestion_status 表
    - update_mr_description（摘要）
    - update_mr_label（风险等级）
11. MySQL 更新 review_tasks（status=done）
```

### Suggestion Apply 处理流程

```
1.  GitCode 推送 push Webhook
2.  遍历 commits[].message，匹配正则 r"Apply \d* ?suggestion"
3.  取 commits[].modified 文件路径列表
4.  查 suggestion_status WHERE file_path IN (...) AND status='open'
5.  更新匹配记录 status='applied'
6.  重算风险等级 → update_mr_label
```

### /ai summary 流程

```
1.  接收 note Webhook，body.strip() == "/ai summary"
2.  调用 get_pr_diff 获取 diff 基础信息（文件数、行数）
3.  查询 MySQL review_results 取该 MR 最近一次检视的 findings
    → 若无历史 findings，直接基于 diff 生成（无问题数据）
4.  SummaryAgent 单次 LLM 调用，输出 SummaryOutput（JSON）
5.  post_inline_comment 将摘要回复到 /ai summary 所在 comment thread
    （不更新 MR 描述，不修改 label）
```

### /ai explain 流程

```
1.  接收 note Webhook，body.strip() == "/ai explain"
2.  从 note.position 提取 file_path + line_number
3.  从父 note body 提取 parent_comment
4.  构建 ExplainRequest（JSON Schema）
5.  ExplainAgent 轻量 ReAct：
    step1: get_file_content(file_path) → 取前后 20 行
    step2: search_team_norms(parent_comment) → 取 top-3 规范片段
    step3: LLM 综合生成 ExplainResponse（JSON）
6.  post_inline_comment 回复到原 thread
```
