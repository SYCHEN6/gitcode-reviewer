# 开发路线图

## 阶段划分

### Phase 1 — 基础链路 ✅

**目标：** MR 创建后能触发检视，结果写回 GitCode inline comment。

- [x] `src/tools/gitcode_client.py` — GitCode v5 REST API 封装
  - 拉取 MR diff / 获取文件内容 / 发送 inline comment + suggestion block
  - 更新 MR 描述 / 查询+设置标签
  - `list_directory()` — 新增文件目录上下文感知
- [x] `src/webhook/main.py` — FastAPI Webhook 入口
  - merge_request / note / push 事件路由
  - Redis 幂等去重（project_id = path_with_namespace）
- [x] `src/mcp/gitcode_server.py` — GitCode MCP Server（FastMCP Streamable HTTP，端口 8081）
- [x] 单 Agent 验证：完整链路跑通
- [x] 验证脚本（`scripts/verify_phase1.py`）：7/7 步通过

**验收：** ✅ 服务已向测试 MR 写回评论

---

### Phase 2 — 多 Agent 核心 ✅

**目标：** 多个专家 Agent 并行分析，结果汇总后写回。

- [x] `src/graph/review_graph.py` — LangGraph StateGraph
  - ReviewState 定义（含 languages / task_id 字段）
  - supervisor / run_agents / synthesize / critic / summary / publish 六个节点
  - `_filter_reviewable_diffs()` — 按扩展名/路径/token 预算过滤
  - `_calc_pr_stats()` — PR 规模分级（small/medium/large/xl）
  - `_detect_languages()` — 从文件扩展名检测编程语言（35+ 种）
  - `_detect_new_files()` — 识别新增文件（status=added 或 @@ -0,0 + hunk）
  - `_enforce_tier_rules()` — Quality/Performance per-file 拆批，large/xl 限批大小
  - `synthesize_node` — 业界标准去重（同 Agent 保留最高 severity，不同 Agent 各自保留）
  - `critic_node` — 行号有效性 + 内容合理性双重过滤
  - `publish_node` — 跨轮去重（三元组 key）+ 结构化摘要更新
  - Redis 分布式并发控制（per-MR 锁 + 全局信号量，支持多实例）
- [x] `src/agents/supervisor.py` — Supervisor Agent
  - LLM 驱动的追查决策（后续轮）
  - 首轮改为规则引擎 + LLM Advisor（get_focus_hints）
- [x] `src/agents/expert_agent.py` — ReAct 循环公共实现
  - 并发预取所有 hunk 上下文 + 新增文件目录列表
  - 多语言检视指引动态注入
  - Token 消耗逐次累计追踪（usage_metadata）
  - tenacity 重试（429/5xx 指数退避，最多 3 次）
  - LLM 路由（deepseek-* → DeepSeek API；其余 → DashScope）
- [x] `src/agents/security_agent.py` / `logic_agent.py` / `quality_agent.py` / `performance_agent.py`
  - 各专项 prompt（多语言通用规则 + 语言无关描述）
  - 统一使用 deepseek-v4-pro 模型
- [x] `src/agents/summary_agent.py` — SummaryAgent（单次 LLM 调用）
- [x] `tests/test_review_logic.py` — 32 个核心逻辑单元测试（全部通过）

**验收：** ✅ 一次 MR 触发 → 多条 inline comment + suggestion block + AI 摘要评论 + 风险标签

---

### Phase 3 — 持久化与命令系统（进行中）

**目标：** 数据落库，Step Checkpoint，命令系统可用。

- [x] `src/db/__init__.py` — DB 模块初始化
- [x] `src/db/repository.py` — SQLAlchemy async（aiomysql）持久化层
  - `create_or_get_task()` — 创建/获取检视任务（支持断点续跑，幂等）
  - `load_agent_findings()` — 加载已完成 Agent 的结果（Checkpoint 恢复）
  - `save_agent_result()` — Agent 完成后立即持久化（含 tokens/duration，ON DUPLICATE KEY UPDATE）
  - `complete_task()` / `fail_task()` — 任务状态流转
  - `init_tables()` — 建表（幂等，服务启动时调用）
- [x] MySQL 建表（review_tasks / review_results / suggestion_status）
  - review_tasks 含 tier / languages / total_files 字段
  - review_results 含 tokens_in / tokens_out / duration_ms 字段
- [x] Checkpoint 集成到 run_review_graph / run_agents_node
  - `create_or_get_task` 在图执行前调用，同 commit 重跑返回同一 task_id（幂等）
  - `run_agents_node` iteration=0 时加载 checkpoint，跳过已完成 Agent 的 LLM 调用
  - 每个 Agent 完成后立即 `save_agent_result`（失败也记录，重跑时再次执行）
  - 批次>1 的 Agent（large PR 拆批）不写 checkpoint，避免并发覆盖
  - `complete_task`/`fail_task` 在图完成/失败时更新任务状态
  - 全链路降级：MYSQL_URL 为空时静默跳过，不影响正常检视流程
- [ ] Suggestion 应用状态追踪（push Webhook 监听）
- [ ] `src/agents/explain_agent.py` — ExplainAgent
- [ ] `/ai review` / `/ai summary` / `/ai explain` / `/ai help` 命令完整实现

**验收标准：** 任务状态正确落库，Agent 结果可查，断点续跑生效，命令系统全部可用

---

### Phase 4 — RAG 知识库（团队规范感知）

**目标：** Agent 检视时能参考团队规范文档，生成符合团队风格的建议。

- [ ] ES 索引创建（`team-norms` + `parent_doc`）
- [ ] `src/tools/ingest_norms.py` — 知识库初始化工具
  - 支持 .md / .txt / .pdf 格式，父子分块策略
  - DashScope Embedding 向量化
- [ ] MCP Server 实现 `search_team_norms()` 工具（混合检索：稠密向量 + BM25）
- [ ] QualityAgent 在 ReAct 循环中集成 RAG 查询
- [ ] Finding.norm_reference 字段正确回填

**验收标准：** 运行 `python -m src.tools.ingest_norms --path ./docs/coding_standards.md` 后，QualityAgent findings 中 `norm_reference` 能引用具体规范内容

---

### Phase 5 — Harness Engine 优化（工程化 & 可扩展性）

**目标：** 对标 Harness Engine 执行理念，提升生产可靠性与可观测性。

**已完成（Phase 2 中同步实现）：**
- [x] tenacity 重试（按错误类型分级：限流退避 / 服务错误重试 / 4xx 不重试）
- [x] Token 消耗追踪（per-agent + per-iteration 日志）
- [x] 分布式并发控制（Redis 分布式锁 + 信号量，多实例安全）
- [x] 多语言支持（35+ 种语言检测 + 动态注入）

**待实现：**
- [ ] **Step Checkpoint 集成**（Phase 3 中实现）：将 repository.py 接入 run_agents_node，跳过已完成 Agent
- [x] **规则引擎派发**（`_rule_engine_dispatch`）+ LLM Advisor（`get_focus_hints`）分离
  - 首轮：规则引擎确定性派遣（0 LLM 成本）+ LLM Advisor 只生成 focus_hints（成本降低 80%+）
  - 后续轮：完整 LLM Supervisor 动态追查
- [x] **结构化 Metrics**：每次检视后写入 Redis（key: `review:metrics:{project}:{mr}:{sha8}`，TTL 7天）
  ```json
  {
    "review_id": "project:mr:sha8",
    "tier": "medium",
    "languages": ["Python", "Go"],
    "agents": { "QualityAgent": { "findings_raw": 3 } },
    "synthesize": { "in": 7, "out": 5 },
    "total_ms": 15000,
    "timestamp": "2026-06-30T10:00:00"
  }
  ```
- [ ] **Per-project 配置**：`project_configs/{project_id}.yaml`，支持 per-project Agent 集合 / findings 上限 / severity 阈值
- [ ] Docker 部署配置（uvicorn + MySQL + Redis + ES 一键启动）
- [ ] 单元测试补充（分布式锁逻辑 / 规则引擎 / DB repository）
- [ ] README 完善（含效果截图 + 快速开始）

---

## 优先级说明

| 阶段 | 优先级 | 状态 |
|------|--------|------|
| Phase 1 基础链路 | P0 | ✅ 完成 |
| Phase 2 多 Agent | P0 | ✅ 完成 |
| Phase 3 持久化+命令 | P1 | 进行中 |
| Phase 4 RAG | P1 | 待开始 |
| Phase 5 Harness Engine 优化 | P2 | 部分完成 |
