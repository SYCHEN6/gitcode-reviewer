# 开发路线图

## 阶段划分

### Phase 1 — 基础链路（目标：跑通端到端流程）

**目标：** MR 创建后能触发检视，结果写回 GitCode inline comment。

- [ ] `src/tools/gitcode_client.py` — GitCode REST API 封装
  - 拉取 MR diff
  - 获取文件内容
  - 发送 inline comment
  - 发送 suggestion block
  - 更新 MR 描述
- [ ] `src/webhook/main.py` — FastAPI Webhook 入口
  - 接收 merge_request 事件
  - 接收 note 事件
  - CommandParser（解析 /ai 命令）
  - Redis 幂等去重
- [ ] `src/mcp/gitcode_server.py` — GitCode MCP Server
  - 基于 python MCP SDK
  - 封装 gitcode_client 为 MCP 工具
- [ ] 单 Agent 验证：用一个简单 Agent 调 MCP 工具跑通完整链路

**验收标准：** 在 GitCode 上创建测试 MR，服务自动写回一条 inline comment。

---

### Phase 2 — 多 Agent 核心（目标：实现 Supervisor + 并行专家 Agent）

**目标：** 多个专家 Agent 并行分析，结果汇总后写回。

- [ ] `src/graph/review_graph.py` — LangGraph StateGraph
  - 定义 ReviewState
  - 定义各节点
  - 配置并行执行
- [ ] `src/agents/supervisor.py` — Supervisor Agent
  - LLM 驱动路由决策
  - 输出 selected_agents
- [ ] `src/agents/security_agent.py` — SecurityAgent
  - ReAct 循环
  - 安全漏洞检测 prompt
  - 逻辑缺陷检测 prompt
- [ ] `src/agents/quality_agent.py` — QualityAgent
  - 代码质量检测
  - 性能问题检测
- [ ] `src/agents/summary_agent.py` — SummaryAgent
  - 变更摘要生成
  - 风险评级算法
- [ ] synthesize_node — 结果合并，按 severity 排序

**验收标准：** 一次 MR 触发，多条问题 inline comment + suggestion block 写回，MR 描述含摘要和风险等级。

---

### Phase 3 — 持久化与命令系统

**目标：** 数据落库，命令系统可用。

- [ ] MySQL 建表（review_tasks / review_results / suggestion_status）
- [ ] 检视任务状态流转（pending → running → done / failed）
- [ ] Suggestion 应用状态追踪（监听 Webhook 更新状态）
- [ ] `src/agents/explain_agent.py` — ExplainAgent
  - 解析被引用代码行
  - 生成解释并回复
- [ ] `/ai review` 命令 — 触发完整重新检视
- [ ] `/ai summary` 命令 — 仅触发 SummaryAgent
- [ ] `/ai explain` 命令 — 触发 ExplainAgent
- [ ] `/ai help` 命令 — 回复命令列表

**验收标准：** 命令系统全部可用，任务状态正确流转，suggestion 应用后状态更新。

---

### Phase 4 — RAG 知识库（团队规范感知）

**目标：** Agent 检视时能参考团队规范文档，生成符合团队风格的建议。

- [ ] ES 索引创建（`team-norms` + `parent_doc`，Mapping 见 ARCHITECTURE.md）
- [ ] `src/tools/ingest_norms.py` — 知识库初始化工具
  - 支持 .md / .txt / .pdf 格式
  - 父子分块策略（父块 800 tokens，子块 150 tokens）
  - DashScope Embedding 向量化
  - 参数：`--path <文档路径>`
- [ ] MCP Server 实现 `search_team_norms(query)` 工具
  - 子块向量检索 → 返回对应父块内容
  - 混合检索：稠密向量 + BM25
- [ ] QualityAgent 在 ReAct 循环中集成 RAG 查询
- [ ] 验证：`norm_reference` 字段正确回填到 Finding

**验收标准：** 运行 `python -m src.tools.ingest_norms --path ./docs/coding_standards.md` 后，QualityAgent 的 Finding 中 `norm_reference` 能引用到具体规范内容。

---

### Phase 5 — 工程化完善

- [ ] 异常处理与重试（LLM 调用失败 / GitCode API 限流）
- [ ] 日志与可观测性
- [ ] 单元测试 + 集成测试
- [ ] Docker 部署配置
- [ ] README 完善（含效果截图）

---

## 优先级说明

| 阶段 | 优先级 | 预计工时 |
|------|--------|---------|
| Phase 1 基础链路 | P0 | 3-4 天 |
| Phase 2 多 Agent | P0 | 5-7 天 |
| Phase 3 持久化+命令 | P1 | 3-4 天 |
| Phase 4 RAG | P1 | 3-4 天 |
| Phase 5 工程化 | P2 | 2-3 天 |

**建议开发顺序：** 先跑通 Phase 1 和 Phase 2，有一个能演示的完整 demo 后再做 Phase 3-4。
