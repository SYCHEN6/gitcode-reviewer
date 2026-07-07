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
  - `publish_node` — 跨轮去重（(file, line_start) 二元组 key）+ 结构化摘要更新
  - Redis 分布式并发控制（per-MR 锁 + 全局信号量，支持多实例）
- [x] `src/agents/supervisor.py` — Supervisor Agent
  - LLM 驱动的追查决策（后续轮）
  - `get_focus_hints` — 首轮 LLM Advisor，仅生成 focus_hint（不做派发决策）
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
- [x] `tests/test_review_logic.py` — 42 个核心逻辑单元测试（全部通过）

**验收：** ✅ 一次 MR 触发 → 多条 inline comment + suggestion block + AI 摘要评论 + 风险标签

---

### Phase 3 — 持久化与命令系统 ✅

**目标：** 数据落库，Step Checkpoint，命令系统可用。

- [x] `src/db/__init__.py` — DB 模块初始化
- [x] `src/db/repository.py` — SQLAlchemy async（aiomysql）持久化层
  - `create_or_get_task()` / `load_agent_findings()` / `save_agent_result()` / `complete_task()` / `fail_task()`
  - `save_suggestion()` — 记录已发布 suggestion（含 comment_id / file_path / line_start）
  - `mark_suggestions_applied()` — push 事件触发，按文件路径批量标记 applied
  - `init_tables()` — 建表 + ALTER TABLE 列补全（幂等，支持旧版本升级）
- [x] MySQL 建表（review_tasks / review_results / suggestion_status）
  - suggestion_status 含 project_id / mr_iid / comment_id / file_path / line_start 字段
- [x] Checkpoint 集成到 run_review_graph / run_agents_node
- [x] Suggestion 应用状态追踪（push Webhook 监听）
  - `handle_push` 检测 "Apply * suggestion" commit，提取变更文件 → `mark_suggestions_applied`
  - `_parse_findings` 在 `expert_agent.py` 中为每条 finding 分配 `finding_id`（`str(uuid.uuid4())`，带连字符格式）
  - `publish_node` 发布 suggestion 评论后写入 `suggestion_status`（status=pending）
- [x] `src/agents/explain_agent.py` — ExplainAgent（单次 LLM，解释指定代码行）
- [x] 命令系统完整实现（`src/webhook/handlers.py`）
  - `/ai review` — 强制重触发完整检视
  - `/ai summary` — 仅生成 PR 摘要（`run_summary_only`，不运行专家 Agent）
  - `/ai explain <file>:<line>[-<end>]` — 解释指定代码片段，结果**追加到原始评论**（edit-in-place，非新评论）
  - `/ai explain` + 代码块 — 直接粘贴代码片段请求解释，AI 解释结果同样追加到原始评论
  - Redis 幂等保护：`explain:{project_id}:{mr_iid}:{note_id}` TTL 24h，防止 Webhook 重复投递
  - `_EXPLAIN_MARKER` HTML 注释防止 Webhook 重复触发（marker 已存在则跳过）
  - `get_pr_comment()` / `update_pr_comment()` 封装 GitCode v5 单条评论读写接口
  - `/ai help` — 发布命令帮助文档

**验收：** ✅ 命令系统全部可用，suggestion 追踪落库，/ai explain 追加到原始评论，74 个单元测试通过

---

### Phase 4 — RAG 知识库（团队规范感知）✅

**目标：** Agent 检视时能参考团队规范文档，生成符合团队风格的建议。

- [x] ES 索引创建（`team-norms` 子块 + `parent_doc` 父块）
  - `team-norms`：dense_vector（1536 dim cosine）+ BM25 text，parent_id 外键
  - `parent_doc`：父块完整文本，mget 批量拉取
- [x] `src/tools/norm_retriever.py` — ES 检索工具（kNN + BM25 混合，父子分块回溯）
  - `ensure_indices()` 幂等建索引
  - `embed_texts()` DashScope Embedding API（text-embedding-v2，1536 dim）
  - `search_norms()` 混合检索：kNN 子块命中 → mget 父块内容
- [x] `src/tools/ingest_norms.py` — 知识库初始化 CLI
  - 支持 .md / .txt 文件 / 目录递归扫描
  - 父块（≈800 token）按 Markdown 章节边界拆分，子块（≈150 token）滑动窗口
  - `--clear` 参数清空重建，批量 embed + bulk index
- [x] MCP Server 添加 `search_team_norms()` 工具（包装 norm_retriever.search_norms）
- [x] QualityAgent 集成 RAG
  - `_make_norm_tool()` 创建 LangChain 工具，ES 不可用时降级（仅保留 file_tool）
  - 工具路由：`tool_map[tc["name"]]` 按名称分发，兼容 file_tool + norm_tool 共存
  - System prompt 指导调用时机 + `norm_reference` 字段填写规范
- [x] Finding.norm_reference 字段由 `_parse_findings` 保留并传播到 publish_node

**验收：** ✅ 运行 `python -m src.tools.ingest_norms --path ./docs/coding_standards.md` 后，QualityAgent findings 中 `norm_reference` 能引用具体规范内容

---

### Phase 5 — Harness Engine 优化（工程化 & 可扩展性）

**目标：** 对标 Harness Engine 执行理念，提升生产可靠性与可观测性。

**已完成（Phase 2 中同步实现）：**
- [x] tenacity 重试（按错误类型分级：限流退避 / 服务错误重试 / 4xx 不重试）
- [x] Token 消耗追踪（per-agent + per-iteration 日志）
- [x] 分布式并发控制（Redis 分布式锁 + 信号量，多实例安全）
- [x] 多语言支持（35+ 种语言检测 + 动态注入）

**待实现：**
- [x] **Step Checkpoint 集成**（Phase 3 中实现）：将 repository.py 接入 run_agents_node，跳过已完成 Agent
- [x] **规则引擎派发**（`_rule_engine_dispatch`，定义于 `review_graph.py`）+ LLM Advisor（`get_focus_hints`，定义于 `supervisor.py`）分离
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
- [x] **Per-project 配置**：`project_configs/{project_id}.yaml`，支持 per-project Agent 集合 / findings 上限 / severity 阈值
  - `project_configs/_default.yaml`：全局默认模板（agents/max_findings/min_severity/max_files）
  - `src/project_config.py`：YAML 加载 + `lru_cache` + `filter_findings_by_config()`
  - 集成到 `supervisor_node`（agent 白名单 + max_files 截断）和 `critic_node`（min_severity + max_findings 过滤）
- [x] Docker 部署配置（uvicorn + MySQL + Redis + ES 一键启动）
  - `Dockerfile`：Python 3.11 slim，依赖缓存层
  - `docker-compose.yml`：app + mcp + mysql + redis + elasticsearch，含 healthcheck
  - `.env.example`：补全所有配置项
- [x] 单元测试补充（分布式锁逻辑 / 规则引擎 / DB repository）
  - `tests/test_concurrency_and_db.py`：20 个测试，覆盖 MR 锁 / 全局信号量 / repository CRUD / per-project 配置
  - 全套 74 个测试全部通过
- [x] README 完善（Docker 快速开始 + per-project 配置说明 + 项目结构树）

---

## 优先级说明

| 阶段 | 优先级 | 状态 |
|------|--------|------|
| Phase 1 基础链路 | P0 | ✅ 完成 |
| Phase 2 多 Agent | P0 | ✅ 完成 |
| Phase 3 持久化+命令 | P1 | ✅ 完成 |
| Phase 4 RAG | P1 | ✅ 完成 |
| Phase 5 Harness Engine 优化 | P2 | ✅ 完成 |
