# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# 启动虚拟环境
.venv/Scripts/activate          # Windows
source .venv/bin/activate       # Linux/Mac

# 安装依赖
uv pip install -r requirements.txt

# 启动 Webhook 服务（主服务，端口 8080）
uvicorn src.webhook.main:app --host 0.0.0.0 --port 8080 --reload

# 启动 MCP Server（独立服务，端口 8081）
python -m src.mcp.gitcode_server

# 知识库初始化（文档 → ES 父子分块入库）
python -m src.tools.ingest_norms --path ./docs/coding_standards.md

# 运行测试
pytest tests/
pytest tests/test_security_agent.py   # 单文件
pytest tests/ -k "test_webhook"       # 按名称过滤
```

## 架构

这是一个 **Multi-Agent PR 自动检视系统**，通过 GitCode Webhook 接入，AI 多 Agent 并行分析 MR diff，结果以 inline comment + suggestion block 写回 GitCode。

### 请求链路

```
GitCode Webhook → FastAPI (src/webhook/main.py)
  ├── merge_request 事件 → LangGraph ReviewOrchestrator (src/graph/review_graph.py)
  ├── push 事件 → SuggestionApplyHandler（匹配 "Apply.*suggestion" commit，更新 label）
  └── note 事件 → CommandParser（/ai review / summary / explain / help）
```

### Multi-Agent 核心：Supervisor 动态循环

**不是 Plan-and-Execute**。Supervisor 采用**规则引擎 + LLM 双层决策**模式，首轮通过确定性规则引擎决定派哪些 Agent，后续轮次 LLM 基于完整 findings 动态追查。

```
【首轮】
_rule_engine_dispatch(files, languages, tier)  ← 确定性，不依赖 LLM
  ↓ 确定最小 Agent 集合（按路径/语言/规模判断）
  + get_focus_hints(...)  ← LLM 仅生成 focus_hint，不控制派发
  ↓ DISPATCH → 并行召唤 Agent（附 hint）

【后续轮】
Supervisor LLM（读取完整 findings + 历史推理）
  ↓ DISPATCH：追查发现的新风险点（精确 focus_hint）
  ↓ FINISH：调查充分，退出循环

SecurityAgent / LogicAgent / QualityAgent / PerformanceAgent（各自独立 ReAct，max 8 轮）
  ↓ findings 通过 operator.add 聚合到 State
  ↓ 回到 Supervisor 再次推理
  ↓ FINISH
  SummaryAgent（单次 LLM 调用）→ synthesize_node → critic_node → publish_node
```

**首轮使用规则引擎的原因**：LLM 决策在边界情况下输出格式不稳定，规则引擎保证 Agent 集合的确定性。`focus_hint` 仍由 LLM 生成，保留语义理解能力。`_enforce_tier_rules` 在 Supervisor 决策之后做结构性纠正（per-file 拆批、文件数限制）。

### 并发控制（Redis 分布式）

系统支持多项目并发 Webhook、多实例横向扩展，通过双层 Redis 分布式机制保证安全：

| 层级 | Redis Key | 机制 | 作用 |
|------|-----------|------|------|
| per-MR 锁 | `review:lock:{project_id}:{mr_iid}` | SET NX + Lua 安全释放 | 同一 MR 多次 push 串行执行，防止并发刷评论 |
| 全局信号量 | `review:semaphore:active` | Lua 原子 INCR+检查 | 跨实例限制总并发数（默认 10，`MAX_CONCURRENT_REVIEWS` 配置） |

两者均有 TTL 兜底（进程崩溃不永久锁死），超时后降级为 warning + 继续执行。

### 多语言支持

`_detect_languages(diffs)` 从变更文件扩展名自动识别编程语言（覆盖 Python / Go / Java / TypeScript / C++ / Rust / SQL 等 35+ 种），结果注入：
- `ReviewState.languages`（供 Supervisor 参考）
- 每个 Agent 的 `initial_msg`（动态语言检视指引）

### 新增文件目录上下文

当 diff 包含新增文件（`status=added`），系统自动：
1. 调用 `GitCodeClient.list_directory()` 获取目标目录现有文件列表
2. 将目录结构注入 Agent 上下文，帮助发现命名冲突、功能重复实现等问题

### 工具分层（严格隔离）

- **读取工具**（专家 Agent ReAct 循环内调用）：`get_pr_diff` / `get_file_content` / `search_team_norms`
- **写入工具**（仅 `publish_node` 调用）：`post_inline_comment` / `post_suggestion` / `update_mr_description` / `update_mr_label`

专家 Agent 绝不直接写 GitCode，所有写回统一由 publish_node 处理，保证结果有序且原子。

### MCP Server

`src/mcp/gitcode_server.py` 封装 GitCode（GitLab v4 兼容）REST API，使用 **Streamable HTTP** 协议（MCP 2025 规范），独立运行在端口 8081。Agent 通过 MCP Client 调用工具，不直接依赖 `python-gitlab`。

### RAG 知识库

`search_team_norms` 工具查询 ES `team-norms` 索引，使用**父子分块**：子块（150 tokens）用于向量检索，命中后返回对应父块（800 tokens）作为 QualityAgent 的 prompt context，结果写入 Finding 的 `norm_reference` 字段。

## JSON Schema 约束

**Agent 间所有交互必须使用 JSON Schema，禁止纯文字传递。** 核心 Schema 定义在 `docs/ARCHITECTURE.md`：

- `SupervisorDecision`：Supervisor 每轮输出，含 `action` / `reasoning` / `agents_to_dispatch`
- `AgentTask`：Supervisor 下发给专家 Agent 的任务，含 `file_chunk` + `diff_slice` + `focus_hint`
- `Finding`：专家 Agent 统一输出格式，含 `finding_id`（UUID）用于 suggestion_status 追踪
- `SummaryOutput` / `ExplainResponse`：对应 Agent 的输出

## 数据持久化

- **MySQL**：`review_tasks`（任务状态 + tier/languages）/ `review_results`（Agent 级输出，含 tokens_in/out/duration_ms）/ `suggestion_status`（建议应用追踪，含 `finding_id` 外键）
  - 通过 `src/db/repository.py`（SQLAlchemy async + aiomysql）访问
  - 支持 Step Checkpoint：每个 Agent 完成后立即写库，重跑时可跳过已完成的 Agent
- **Redis**：
  - 幂等 key：`review:{project_id}:{mr_iid}:{commit_sha}`，TTL 24h；`/ai review` 命令用 `cmd:{timestamp}` 后缀绕过幂等
  - MR 分布式锁：`review:lock:{project_id}:{mr_iid}`，TTL 1h，SET NX + Lua 安全释放
  - 全局信号量：`review:semaphore:active`，Lua 原子 INCR/DECR，TTL 1h
- **ES**：`team-norms`（子块向量 + parent_id）+ `parent_doc`（父块原文）

## 关键约束

- `post_inline_comment` 的 `position` 对象（`head_sha` / `new_path` / `new_line`）必须来自当次 `get_pr_diff` 返回，publish_node 负责提前获取
- synthesize_node 去重逻辑：同 Agent 同行 → 保留最高 severity；不同 Agent 同行 → 全部保留（业界标准：各自独立 inline comment）；跨 Agent 描述前 40 字相同 → 去重
- critic_node 过滤标准：`_nearest_added_line` 验证行号在 diff `+` 行上；`_description_plausible` 验证关键词与实际代码一致
- publish_node 跨轮去重键：`(file, line_start, description[:40])` 三元组，不是 `(file, line_start)` 双元组（同行不同描述应各自发评论）
- 单个专家 Agent 失败不中断整体流程，SummaryAgent 的 `focus_points` 中追加警告，`review_results.status` 记为 `failed`
- Webhook 必须验证 `X-Gitcode-Token` header，值来自 `.env` 的 `WEBHOOK_SECRET`
- LLM 路由规则：模型名 `deepseek-*` 走 DeepSeek API（`DEEPSEEK_BASE_URL` / `DEEPSEEK_API_KEY`），其余走 DashScope
