# GitCode AI Reviewer

基于多智能体架构的 GitCode PR 自动化代码检视系统。

## 项目简介

通过 GitCode Webhook 接入，在合并请求创建或更新时自动触发 AI 检视。系统由多个专家 Agent 并行分析代码，生成内联评论、可直接应用的代码建议和变更摘要，帮助开发者快速发现安全漏洞、逻辑缺陷、质量问题和性能隐患。

## 功能特性

### 1. 智能检视摘要
合并请求创建后自动生成变更摘要，包含：
- 变更文件数量与代码行数统计
- 改动范围与影响面分析
- 风险等级评估（低 / 中 / 高 / 危急）
- 主要关注点提示

### 2. 代码问题检测
自动检测四类问题，按严重程度排序输出：
- **安全风险**：SQL 注入、硬编码密钥、密码明文存储
- **逻辑缺陷**：空指针引用、资源泄露、竞态条件
- **代码质量**：过长函数、重复代码、命名不规范
- **性能问题**：N+1 查询、不必要循环、内存浪费

### 3. 可应用的代码建议
- 问题以 inline comment 形式标注在具体代码行
- 修复建议以 suggestion block 展示，开发者点击即可 apply
- 建议应用后自动更新检视状态

### 4. 命令系统
在合并请求评论区输入命令主动触发 AI 协助：

| 命令 | 说明 |
|------|------|
| `/ai review` | 对当前 MR 重新进行完整检视 |
| `/ai summary` | 仅生成变更摘要，不重新检视 |
| `/ai explain` | 解释指定代码片段或评论 |
| `/ai help` | 显示所有可用命令 |

## 技术栈

| 组件 | 技术选型 |
|------|---------|
| 语言 | Python 3.11 |
| Agent 框架 | LangGraph |
| LLM | Qwen2.5-Coder（DashScope） |
| Web 框架 | FastAPI |
| 向量检索 | Elasticsearch（混合检索） |
| 关系数据库 | MySQL |
| 缓存 / 去重 | Redis |
| 工具协议 | MCP（自建 GitCode MCP Server） |
| GitCode 交互 | python-gitlab |

## 快速开始

### 方式一：Docker 一键启动（推荐）

```bash
# 克隆项目
git clone <repo-url>
cd gitcode-reviewer

# 配置环境变量（填入 LLM API Key 和 GitCode Token）
cp .env.example .env
vim .env

# 启动所有服务（MySQL + Redis + Elasticsearch + Webhook 服务 + MCP Server）
docker compose up -d

# 查看服务状态
docker compose ps

# 查看日志
docker compose logs -f app
```

> 首次启动时 Elasticsearch 需要约 30 秒初始化，app 服务会等待其健康后再启动。

### 方式二：本地开发

```bash
# 创建虚拟环境并安装依赖
uv venv
uv pip install -r requirements.txt

# 配置环境变量
cp .env.example .env
# 编辑 .env 填入 API Key 等配置

# 启动 Webhook 服务（端口 8080）
uvicorn src.webhook.main:app --host 0.0.0.0 --port 8080 --reload

# 可选：启动 MCP Server（端口 8081）
python -m src.mcp.gitcode_server
```

## Per-project 配置

在 `project_configs/` 目录下为每个项目创建专属配置文件（文件名格式：`{owner}__{repo}.yaml`）：

```bash
# 以 chensiyu47/MindIE-SD 为例
cp project_configs/_default.yaml project_configs/chensiyu47__MindIE-SD.yaml
vim project_configs/chensiyu47__MindIE-SD.yaml
```

支持以下配置项：

| 字段 | 说明 | 默认值 |
|------|------|--------|
| `agents` | 允许运行的 Agent 列表（空 = 由规则引擎决定） | `[]` |
| `max_findings` | 每次检视最多输出的问题数（0 = 不限制） | `30` |
| `min_severity` | 最低 severity 阈值（`CRITICAL`/`HIGH`/`MEDIUM`/`LOW`） | `LOW` |
| `max_files` | 每次检视最多分析的文件数（0 = 不限制） | `0` |

示例（只运行安全检查 + 逻辑检查，不输出 LOW 级别问题）：

```yaml
agents:
  - SecurityAgent
  - LogicAgent
min_severity: MEDIUM
max_findings: 20
```

## 项目结构

```
gitcode-reviewer/
├── src/
│   ├── agents/              # 各专家 Agent 实现
│   │   ├── supervisor.py    # Supervisor Agent（LLM 驱动路由）
│   │   ├── security_agent.py
│   │   ├── quality_agent.py
│   │   ├── summary_agent.py
│   │   └── explain_agent.py
│   ├── graph/
│   │   └── review_graph.py  # LangGraph StateGraph 定义
│   ├── mcp/
│   │   └── gitcode_server.py  # GitCode MCP Server
│   ├── tools/
│   │   ├── gitcode_client.py    # GitCode REST API 封装
│   │   ├── norm_retriever.py    # ES 知识库检索（kNN + BM25 混合）
│   │   └── ingest_norms.py      # 知识库初始化 CLI
│   ├── db/
│   │   └── repository.py        # SQLAlchemy async 持久化层
│   ├── project_config.py        # Per-project 配置加载
│   └── webhook/
│       ├── main.py              # FastAPI 入口
│       └── handlers.py          # Webhook 事件处理 + 命令系统
├── project_configs/
│   └── _default.yaml            # Per-project 配置模板
├── docs/
│   ├── ARCHITECTURE.md          # 详细架构设计
│   └── ROADMAP.md               # 开发路线图
├── tests/
│   ├── test_review_logic.py     # 核心逻辑 54 个测试
│   ├── test_concurrency_and_db.py  # 并发控制 + DB 20 个测试
│   ├── test_gitcode_client.py   # GitCode API 客户端测试
│   └── test_webhook.py          # Webhook 入口测试
├── Dockerfile
├── docker-compose.yml
├── .env.example
└── requirements.txt
```

详细架构见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
