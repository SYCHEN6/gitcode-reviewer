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
│  │  【首轮】规则引擎 + LLM 双层决策                           │   │
│  │  ┌──────────────────────────────────────────────────┐   │   │
│  │  │ _rule_engine_dispatch(files, languages, tier)    │   │   │
│  │  │ → 确定性派发（Security/Logic/Quality/Performance）│   │   │
│  │  │ + get_focus_hints() LLM → focus_hint per agent  │   │   │
│  │  └──────────────────┬───────────────────────────────┘   │   │
│  │                     │                                    │   │
│  │  【后续轮】Supervisor LLM 全权追查                        │   │
│  │  ┌──────────────────────────────────────────────────┐   │   │
│  │  │ Supervisor（读 findings）→ DISPATCH / FINISH     │◀──┐│   │
│  │  └──────────────────┬───────────────────────────────┘   ││   │
│  │         ┌───────────┴───────────┐                       ││   │
│  │    DISPATCH                  FINISH                     ││   │
│  │         ▼                       ▼                       ││   │
│  │  并行召唤 Agent             退出循环                      ││   │
│  │  ┌────────┬────────┬────────┬────────┐                  ││   │
│  │  ▼        ▼        ▼        ▼        │                  ││   │
│  │ Security Logic  Quality Performance  │                  ││   │
│  │  Agent   Agent   Agent    Agent      │                  ││   │
│  │ (ReAct) (ReAct) (ReAct)  (ReAct)    │                  ││   │
│  │  ┌──────────────────────────────────┘                  ││   │
│  │  │ findings 汇入 State（operator.add 聚合）              │┘   │
│  │  └───────────────────────────────────────────────────────┘   │
│  └──────────────────────────────────────────────────────────┘   │
│                       │ FINISH                                   │
│                       ▼                                         │
│  synthesize_node  去重（per-agent 最高 severity + 描述去重）      │
│                       ▼                                         │
│    critic_node   行号验证 + 内容合理性过滤（Reflection）          │
│                       ▼                                         │
│              ┌─────────────────┐                                │
│              │  Summary Agent  │  单次 LLM 调用                  │
│              └────────┬────────┘                                │
│                       ▼                                         │
│     publish_node  统一写回 GitCode（跨轮去重 + 结构化摘要）       │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│          GitCode MCP Server（Streamable HTTP 协议）               │
│                                                                 │
│  读取工具（专家 Agent ReAct 循环调用）                            │
│  ├── get_pr_diff(project_id, mr_iid)                            │
│  ├── get_file_content(project_id, path, ref)                    │
│  ├── list_directory(project_id, dir_path, ref)                  │
│  └── search_team_norms(query)                                   │
│                                                                 │
│  写入工具（仅 publish_node 调用）                                 │
│  ├── post_inline_comment(project_id, mr_iid, path, line, body)  │
│  ├── post_suggestion(project_id, mr_iid, path, line, code)      │
│  ├── update_mr_description(project_id, mr_iid, body)            │
│  └── update_mr_label(project_id, mr_iid, labels)               │
└─────────────────────────────────────────────────────────────────┘

┌──────────────────┐  ┌──────────────────────────┐  ┌────────────────────┐
│      MySQL       │  │          Redis            │  │  Elasticsearch     │
│                  │  │                          │  │                    │
│  review_tasks    │  │ 幂等 key（TTL 24h）       │  │ 团队规范向量索引    │
│  review_results  │  │ MR 分布式锁（TTL 1h）     │  │ 历史 PR 知识库     │
│  suggestion_     │  │ 全局信号量（TTL 1h）      │  │ （混合检索 RAG）   │
│    status        │  │                          │  │                    │
└──────────────────┘  └──────────────────────────┘  └────────────────────┘
```

---

## JSON Schema 规范

> Agent 间所有交互、Agent 内部每步输入输出均使用以下 Schema，禁止纯文字传递。

### Supervisor 输出 Schema（每轮动态决策）

> **首轮**：`supervisor_node` 通过规则引擎生成 `agents_to_dispatch`，调用 LLM 仅补充 `focus_hint`。
> **后续轮**：Supervisor LLM 完整输出此 Schema，控制 DISPATCH/FINISH 决策。

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
      "description": "本轮决策依据，记录在 State 中供审计"
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
            "description": "基于上一轮 findings 的专项提示，首轮由 LLM Advisor 生成"
          }
        }
      }
    }
  }
}
```

### 专家 Agent 任务输入 Schema（supervisor_node → 各 Agent）

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "AgentTask",
  "type": "object",
  "required": ["agent_type", "project_id", "mr_iid", "files", "diff_slice"],
  "properties": {
    "agent_type":  { "type": "string", "enum": ["SecurityAgent","LogicAgent","QualityAgent","PerformanceAgent"] },
    "project_id":  { "type": "string", "description": "owner/repo 格式" },
    "mr_iid":      { "type": "integer" },
    "files":       { "type": "array", "items": { "type": "string" }, "description": "Agent 负责的文件列表" },
    "diff_slice":  { "type": "string", "description": "对应文件的 diff 片段（由 run_agents_node 填充）" },
    "focus_hint":  { "type": "string", "description": "Supervisor 给出的专项调查提示" },
    "new_files":   { "type": "array", "items": { "type": "string" }, "description": "本批次中新增文件（status=added），需预取目录结构" },
    "languages":   { "type": "array", "items": { "type": "string" }, "description": "PR 变更文件检测到的编程语言列表" }
  }
}
```

### 专家 Agent Finding Schema（各 Agent → State）

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "Finding",
  "type": "object",
  "required": ["finding_id", "agent", "severity", "category", "file", "line_start", "description"],
  "properties": {
    "finding_id":       { "type": "string", "description": "UUID，用于 suggestion_status 追踪" },
    "agent":            { "type": "string", "enum": ["SecurityAgent","LogicAgent","QualityAgent","PerformanceAgent"] },
    "severity":         { "type": "string", "enum": ["CRITICAL","HIGH","MEDIUM","LOW"] },
    "category":         { "type": "string", "enum": ["security", "logic", "quality", "performance"] },
    "file":             { "type": "string" },
    "line_start":       { "type": "integer", "description": "新文件行号（diff + 行），不是 context 行" },
    "line_end":         { "type": "integer" },
    "diff_position":    { "type": "integer", "description": "diff 行偏移量，由 publish_node 计算" },
    "description":      { "type": "string", "description": "问题描述，面向开发者" },
    "suggestion_code":  { "type": "string", "description": "建议替换的代码；null=无建议；\"\"=删除该行" },
    "norm_reference":   { "type": "string", "description": "引用的团队规范片段（QualityAgent 专用）" }
  }
}
```

### SummaryAgent 输出 Schema

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "SummaryOutput",
  "type": "object",
  "required": ["total_files", "impact_analysis", "risk_level", "focus_points"],
  "properties": {
    "total_files":      { "type": "integer" },
    "impact_analysis":  { "type": "string" },
    "risk_level":       { "type": "string", "enum": ["LOW", "MEDIUM", "HIGH", "CRITICAL"] },
    "risk_reason":      { "type": "string" },
    "focus_points":     { "type": "array", "items": { "type": "string" }, "description": "最多 5 条" }
  }
}
```

---

## LangGraph State 定义

```python
from typing import TypedDict, Annotated
import operator

class ReviewState(TypedDict):
    # ── 输入 ──────────────────────────────────────────────────────
    project_id:  str   # "owner/repo" 格式
    mr_iid:      int
    commit_sha:  str

    # ── init 阶段填充 ─────────────────────────────────────────────
    raw_diff:   str
    file_list:  list[str]
    diffs:      list[dict]     # 原始 diff 结构（含 status/patch 等）
    head_sha:   str
    base_sha:   str
    pr_stats:   dict           # {files, lines_added, lines_removed, tier}
    languages:  list[str]      # 从文件扩展名检测出的编程语言列表
    task_id:    int            # MySQL review_tasks.id（DB 可用时填充）

    # ── Supervisor 循环控制 ───────────────────────────────────────
    iteration:            int
    supervisor_action:    str  # DISPATCH / FINISH
    supervisor_reasoning: Annotated[list[str], operator.add]
    pr_meta:              dict
    agents_to_dispatch:   list[dict]

    # ── 专家 Agent 聚合输出 ───────────────────────────────────────
    findings: Annotated[list[dict], operator.add]

    # ── 最终输出 ──────────────────────────────────────────────────
    summary:        dict   # SummaryOutput
    final_findings: list[dict]
```

---

## 规则引擎派发逻辑（首轮）

`_rule_engine_dispatch(files, languages, tier, pr_stats)` 基于以下规则确定最小 Agent 集合：

| 触发条件 | 必含 Agent |
|---------|-----------|
| 路径含 auth/login/token/password/crypto/jwt/oauth | SecurityAgent |
| 语言含 SQL | SecurityAgent |
| 路径含 train/inference/model/layer/attn/flash/cuda/gpu | PerformanceAgent |
| 语言含 C++/Rust/C | PerformanceAgent |
| tier = large / xl | PerformanceAgent |
| 所有情况 | QualityAgent + LogicAgent |
| tier = medium / large / xl | 所有 4 个 Agent |

`_enforce_tier_rules` 在派发决策之后做结构性纠正：
- QualityAgent / PerformanceAgent：超过 1 个文件则按文件拆批（保证每文件独立覆盖）
- large / xl tier：所有 Agent 每批最多 5 个文件

---

## 大 PR 处理：文件拆批并行

```
整个 diff（过滤后可检视文件）
    ↓
_filter_reviewable_diffs()
  - 跳过：图片/二进制/lock/生成物（按扩展名+路径）
  - soft threshold（~640K chars）：记录警告继续
  - hard threshold（~752K chars）：文件级停止（不截断文件内部）
    ↓
_calc_pr_stats() → tier: small / medium / large / xl
    ↓
_rule_engine_dispatch() → base Agent 集合
    ↓
_enforce_tier_rules() → 文件拆批（Quality/Performance per-file，large/xl 每批≤5文件）
    ↓
asyncio.gather(*[run_agent(task) for task in tasks])
  每个 Agent session：
    - 并发预取所有 hunk 上下文 + 新增文件目录列表
    - ReAct 循环（max_iterations=8）
    - Token 消耗逐次累计，完成时输出 [agent] tokens(in=X out=Y total=Z)
    ↓
findings 通过 operator.add 聚合到 State
```

---

## 并发控制（分布式，支持多实例）

```
Webhook 触发
  ↓
Redis 幂等检查：review:{project_id}:{mr_iid}:{commit_sha}
  存在 → skip（同 commit 不重复检视）
  不存在 → setex(key, 24h)
  ↓
BackgroundTasks.add_task(run_review_graph, ...)
  ↓
run_review_graph 内部：
  ① 获取 per-MR 分布式锁：review:lock:{project_id}:{mr_iid}
     SET NX PX 3600000（1h TTL 兜底）
     等待 120s，超时后 warning + 继续
  ② 获取全局分布式信号量：review:semaphore:active
     Lua 原子 INCR + 检查 ≤ MAX_CONCURRENT_REVIEWS
     等待 60s，超时后 warning + 继续
  ③ 执行 _graph.ainvoke(initial)
  ④ 释放信号量（DECR）
  ⑤ 释放 MR 锁（Lua：仅当 owner 匹配时 DEL）
```

---

## 核心模块说明

### 1. Webhook Gateway（`src/webhook/main.py`）

**端点：**
- `POST /webhook` — 接收所有 GitCode Webhook 事件（需验证 Secret Token）
- `GET /health` — 健康检查

**幂等保障：**

| 触发来源 | Redis Key | 行为 |
|---------|-----------|------|
| MR 自动触发 | `review:{project_id}:{mr_iid}:{commit_sha}` | 存在则跳过，防止同一 commit 重复检视 |
| `/ai review` 命令 | `review:{project_id}:{mr_iid}:cmd:{timestamp}` | 每次命令生成新 key，始终执行 |
| `/ai explain` 命令 | `explain:{project_id}:{mr_iid}:{note_id}` | 存在则跳过，防止 Webhook 重复投递 |

**`/ai explain` — edit-in-place 机制：**

GitCode v5 无评论 threading/reply API，所有 reply 调用均生成独立评论。为避免 AI 解释和用户命令分裂成两条评论，采用 edit-in-place 策略：

1. 调用 `get_pr_comment(note_id)` 获取原始评论内容
2. 检查是否已含 `_EXPLAIN_MARKER`（`<!-- __AI_EXPLAIN_APPENDED_7f3a__ -->`）
3. 未含 marker → 拼接 `{原文}\n\n{marker}\n\n{AI 解释}` → `update_pr_comment()` 就地修改
4. 已含 marker → 幂等跳过（防止并发重入后的重复追加）
5. `update_pr_comment` 失败时降级 → `post_mr_note` 发布独立评论

LLM 输出在写入前做 `.replace(_EXPLAIN_MARKER, "")` 清洗，防止 LLM 自行在输出中生成该 marker 导致误判。

---

### 2. Supervisor 层（`src/agents/supervisor.py`）

**职责分离：**

| 函数 | 调用时机 | 职责 |
|------|---------|------|
| `_rule_engine_dispatch()` | 首轮，在 `supervisor_node` 内 | 确定性派发，不调 LLM |
| `get_focus_hints()` | 首轮，配合规则引擎 | 仅生成 focus_hint，单次 LLM 调用 |
| `run_supervisor()` | 后续轮（iteration≥1） | 完整 DISPATCH/FINISH 决策，LLM 读取 findings |

**决策规则（首轮规则引擎）：**
- 文件路径含安全相关词 → SecurityAgent
- 文件路径含 ML/系统性能词 / 语言含 C++/Rust → PerformanceAgent
- tier=medium+ → 全量 4 Agent
- 始终包含：QualityAgent + LogicAgent

---

### 3. 专家 Agent 设计（`src/agents/expert_agent.py`）

所有专家 Agent 基于 `run_expert_agent()` 实现，具备：

- **ReAct 循环**（最多 8 轮）：`get_file_content` 工具调用 → 推理 → 输出
- **并发预取**：`asyncio.gather` 同时拉取所有 hunk 上下文 + 新增文件目录列表
- **Token 追踪**：每次 `llm.ainvoke()` 后累计 `usage_metadata`，完成时记录
  ```
  [QualityAgent] done: iter=3 findings=4 tokens(in=28450 out=1230 total=29680)
  ```
- **重试策略（tenacity）**：HTTP 429/5xx/网络错误 → 指数退避重试（max 3次）；4xx/解析错误 → 不重试直接降级
- **多语言注入**：从 `task.languages` 动态生成语言检视指引，注入 `initial_msg`
- **新增文件上下文**：为 `task.new_files` 预取目录列表，帮助发现命名冲突

| Agent | 文件 | 检测范围 | 模型 |
|-------|------|---------|------|
| SecurityAgent | `security_agent.py` | SQL注入、硬编码密钥、明文密码 | deepseek-v4-pro |
| LogicAgent | `logic_agent.py` | 空指针、资源泄露、竞态、边界条件 | deepseek-v4-pro |
| QualityAgent | `quality_agent.py` | 过长函数、重复代码、魔法数字、调试残留 | deepseek-v4-pro |
| PerformanceAgent | `performance_agent.py` | N+1、内存、同步I/O、ML热路径 | deepseek-v4-pro |

Supervisor 使用 qwen（DashScope）。

---

### 4. synthesize_node（去重逻辑）

对所有 Agent findings 执行：

1. **同 Agent 同行去重**：`key=(agent, file, line_start)` → 保留最高 severity 那条
2. **跨 Agent 同行保留**：不同 Agent 发现同一行 → 全部保留（业界标准，各自独立 comment）
3. **描述去重**：`key=(file, line_start, description[:40])` → 跨 Agent 相同描述只保留一条
4. **排序**：CRITICAL → HIGH → MEDIUM → LOW

---

### 5. critic_node（质量过滤）

逐条 finding 执行以下验证，任意不通过则丢弃：

1. **行号有效性**：`_nearest_added_line(patch, line_start)` → 必须是 diff 中的 `+` 行
2. **内容合理性**：`_description_plausible(description, code_range)` → description 声称存在的关键词（如 `print`）必须在实际代码区间内出现

---

### 6. publish_node（跨轮去重 + 写回）

1. 拉取 PR 现有评论，通过 `_parse_reported_keys()` 提取已报告的二元组：`(file, line_start)`
2. 只对新发现（(file, line_start) 二元组不在已报告集合中）发布 inline comment
3. 发布格式：`{emoji} **[{SEVERITY}]** \`{file}:{line}\`\n\n{description}`，有建议时附 suggestion block
4. 更新/创建 AI 摘要评论（含问题清单 + 风险等级 + 统计）
5. 设置 `ai-risk-high` / `ai-risk-low` 标签

---

### 7. 数据库持久化（`src/db/repository.py`）

**异步访问**：SQLAlchemy async engine + aiomysql 驱动
**连接字符串**：自动将 `mysql+pymysql://` 转换为 `mysql+aiomysql://`

**Step Checkpoint 流程**：
1. `run_review_graph` 入口：`create_or_get_task()` → 获取 `task_id`（支持同 commit 续跑）
2. `run_agents_node` 执行前：`load_agent_findings(task_id)` → 跳过已完成的 Agent
3. 每个 Agent 完成后：`save_agent_result()` → 立即持久化（含 findings JSON + token 统计）
4. `publish_node` 完成后：`complete_task(task_id)`

---

### 8. GitCode Client（`src/tools/gitcode_client.py`）

封装 GitCode v5 REST API（`/api/v5/repos/{owner}/{repo}/...`），主要接口：

| 方法 | 用途 |
|------|------|
| `get_pr_diff` | 获取 PR diff + SHA 信息 |
| `get_file_content` | 获取文件内容（支持 ref） |
| `list_directory` | 获取目录文件列表（新增文件上下文感知） |
| `post_inline_comment` | 发送行内评论 |
| `post_suggestion` | 发送代码建议 block |
| `post_mr_note` | 发送 PR 全局评论（无 position） |
| `get_pr_comment` | 获取单条评论内容（`/ai explain` edit-in-place 用） |
| `get_pr_comments` | 获取现有评论（跨轮去重用） |
| `update_pr_comment` | 编辑已有评论（`/ai explain` 追加解释用） |
| `update_mr_label` | 设置风险等级标签 |

---

### 9. 多语言支持

**语言检测**：`_detect_languages(diffs)` 从文件扩展名检测，覆盖 35+ 种语言：

| 语言族 | 扩展名 |
|--------|--------|
| Python 生态 | .py .pyi .pyx |
| JVM | .java .kt .scala .groovy |
| Go | .go |
| Web 前端 | .js .ts .tsx .jsx .vue .svelte |
| 系统语言 | .c .cpp .cc .h .hpp .rs .swift |
| 脚本 | .rb .php .sh .lua .dart |
| 数据/配置 | .sql .proto .tf .yaml .toml |

**注入方式**：检测结果以语言检视指引注入每个 Agent 的 `initial_msg`，LLM 自动应用对应语言的最佳实践。

---

## 数据库设计

### review_tasks

```sql
CREATE TABLE review_tasks (
    id          BIGINT AUTO_INCREMENT PRIMARY KEY,
    project_id  VARCHAR(200) NOT NULL COMMENT 'owner/repo 格式',
    mr_iid      INT NOT NULL,
    commit_sha  VARCHAR(64) NOT NULL,
    status      ENUM('running','completed','failed') NOT NULL DEFAULT 'running',
    tier        VARCHAR(20) COMMENT 'small/medium/large/xl',
    languages   JSON COMMENT '检测到的编程语言列表',
    total_files INT DEFAULT 0,
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at  DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_review (project_id(191), mr_iid, commit_sha(64)),
    INDEX idx_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

### review_results

```sql
CREATE TABLE review_results (
    id          BIGINT AUTO_INCREMENT PRIMARY KEY,
    task_id     BIGINT NOT NULL,
    agent_type  VARCHAR(50) NOT NULL COMMENT 'SecurityAgent / LogicAgent / ...',
    status      ENUM('completed','failed','skipped') NOT NULL DEFAULT 'completed',
    findings    JSON COMMENT '符合 Finding Schema 的数组',
    tokens_in   INT DEFAULT 0,
    tokens_out  INT DEFAULT 0,
    duration_ms INT DEFAULT 0,
    error_msg   VARCHAR(500),
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uk_task_agent (task_id, agent_type(50)),
    FOREIGN KEY (task_id) REFERENCES review_tasks(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

### suggestion_status

```sql
CREATE TABLE suggestion_status (
    id           INT          NOT NULL AUTO_INCREMENT PRIMARY KEY,
    task_id      VARCHAR(64)  NOT NULL,
    finding_id   VARCHAR(64)  NOT NULL COMMENT 'Finding.finding_id UUID',
    project_id   VARCHAR(255),
    mr_iid       INT,
    comment_id   BIGINT,
    file_path    VARCHAR(500),
    line_start   INT,
    severity     VARCHAR(20)  DEFAULT 'LOW',
    status       VARCHAR(20)  NOT NULL DEFAULT 'pending',
    applied_at   DATETIME,
    UNIQUE KEY   uq_finding (finding_id),
    INDEX        idx_task (task_id),
    INDEX        idx_project_mr (project_id, mr_iid)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

**风险等级重算逻辑（suggestion apply 后触发）：**
```
push Webhook → _mark_suggestions_applied(project_id, file_paths)
  → get_open_mr_ids(project_id, file_paths)   # apply 前查出涉及的 MR
  → mark_suggestions_applied()                 # 更新 status=applied
  → 对每个 mr_iid：
      count_open_critical_high(project_id, mr_iid)  # 剩余 CRITICAL/HIGH pending 数 N
      N > 0 : update_mr_label(['ai-risk-high'])
      N == 0 : update_mr_label(['ai-risk-low'])
```

---

## 关键流程

### MR 自动检视流程

```
1.  GitCode → merge_request Webhook（X-Gitcode-Token 验证）
2.  Redis 幂等检查（project_id = path_with_namespace）
3.  获取 per-MR 分布式锁（review:lock:{project_id}:{mr_iid}）
4.  获取全局信号量（review:semaphore:active ≤ MAX_CONCURRENT_REVIEWS）
5.  MySQL review_tasks 创建/获取（支持断点续跑）
6.  get_pr_diff → 过滤不可检视文件 → 检测编程语言
7.  【首轮】规则引擎确定 Agent 集合 + LLM Advisor 生成 focus_hint
8.  _enforce_tier_rules 文件拆批（Quality/Performance per-file）
9.  asyncio.gather 并行执行所有 Agent（ReAct 循环，max 8 次）
    - 并发预取 hunk 上下文 + 新增文件目录列表
    - Token 追踪（usage_metadata 累计）
    - tenacity 重试（429/5xx 指数退避，最多 3 次）
    - 每个 Agent 完成后写 review_results
10. findings 通过 operator.add 聚合到 State
11. 【可选第 2 轮追查】Supervisor LLM 读 findings → DISPATCH 追查 / FINISH
12. synthesize_node 去重排序 → critic_node 质量过滤
13. summary_node 生成检视摘要（SummaryOutput JSON）
14. publish_node：
    - 跨轮去重（拉现有评论，(file, line_start) 二元组比对；同行 LLM 措辞不同也视为重复）
    - 发布 inline comment + suggestion block
    - 更新/创建 AI 摘要评论
    - 设置风险标签
15. MySQL 更新 review_tasks.status = 'completed'
16. 释放分布式锁和信号量
```

### Suggestion Apply 处理流程

```
1.  push Webhook → 匹配 commit msg r"Apply \d* ?suggestion"
2.  get_open_mr_ids(project_id, file_paths) → 获取涉及的 MR 列表
3.  mark_suggestions_applied() → 更新 status='applied'，记录 applied_at
4.  对每个 mr_iid：count_open_critical_high() → 重算 ai-risk 标签
```
