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

```bash
# 克隆项目
git clone <repo-url>
cd gitcode-reviewer

# 创建虚拟环境并安装依赖
uv venv
uv pip install -r requirements.txt

# 配置环境变量
cp .env.example .env
# 编辑 .env 填入 API Key 等配置

# 启动服务
uvicorn src.webhook.main:app --host 0.0.0.0 --port 8080 --reload
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
│   │   ├── gitcode_client.py  # GitCode REST API 封装
│   │   └── ingest_norms.py    # 知识库初始化（文档 → ES 父子分块入库）
│   └── webhook/
│       └── main.py            # FastAPI 入口 + Webhook 处理
├── docs/
│   ├── ARCHITECTURE.md        # 详细架构设计
│   └── ROADMAP.md             # 开发路线图
├── tests/
├── .env
└── requirements.txt
```

详细架构见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
