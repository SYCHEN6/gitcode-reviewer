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

**不是 Plan-and-Execute**。Supervisor 每轮读取完整 State（含历史 findings）后用 LLM 动态决策，输出 `DISPATCH`（继续）或 `FINISH`（结束）。

```
Supervisor（每轮 LLM 推理，最多 5 轮）
  ↓ DISPATCH：按本轮决策并行召唤 Agent
  SecurityAgent / LogicAgent / QualityAgent / PerformanceAgent（各自独立 ReAct，max 8 轮）
  ↓ findings 通过 operator.add 聚合到 State
  ↓ 回到 Supervisor 再次推理
  ↓ FINISH
  SummaryAgent（单次 LLM 调用）→ synthesize_node → critic_node → publish_node
```

`focus_hint` 字段是 Agent 协作的关键：Supervisor 根据上一轮某 Agent 的发现，给下一轮指定 Agent 传递精确的调查方向。

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

- **MySQL**：`review_tasks`（任务状态）/ `review_results`（Agent 级输出）/ `suggestion_status`（建议应用追踪，含 `finding_id` 外键）
- **Redis**：幂等 key `review:{project_id}:{mr_iid}:{commit_sha}`，TTL 24h；`/ai review` 命令用 `cmd:{timestamp}` 后缀绕过幂等
- **ES**：`team-norms`（子块向量 + parent_id）+ `parent_doc`（父块原文）

## 关键约束

- `post_inline_comment` 的 `position` 对象（`base_sha` / `start_sha` / `head_sha`）必须来自当次 `get_pr_diff` 返回，publish_node 负责提前获取
- synthesize_node 去重键是 `(file, line_start, category)`，不是 `finding_id`（同一问题在不同 chunk 有不同 UUID）
- 单个专家 Agent 失败不中断整体流程，SummaryAgent 的 `focus_points` 中追加警告，`review_results.status` 记为 `failed`
- Webhook 必须验证 `X-Gitlab-Token` header，值来自 `.env` 的 `WEBHOOK_SECRET`
