"""Supervisor Agent：每轮读取完整 State，用 LLM 决策下一步行动。"""

import json
import logging
import re

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from src.config import settings

logger = logging.getLogger(__name__)

_SYSTEM = """你是 PR 代码检视的协调者（Supervisor），负责根据 PR 规模动态调度专家 Agent 进行深度检视。

## 可用专家 Agent
- SecurityAgent：检测安全漏洞（SQL 注入 / 硬编码密钥 / XSS / SSRF 等）
- LogicAgent：检测逻辑缺陷（空指针 / 资源泄露 / 竞态 / 边界条件等）
- QualityAgent：检测代码质量（命名 / 重复代码 / 过长函数 / 嵌套过深等）
- PerformanceAgent：检测性能问题（N+1 / 全量加载 / 同步 I/O / ML 热路径等）

## PR 规模分级策略（首轮必须遵守）
阈值来源：SmartBear/Cisco 研究 + PropelCode 50,000+ PR 数据集。

**small**（≤50 行 / ≤3 文件）：根据代码类型选择 Agent，**不强制限制为 Quality+Logic**。
- 纯配置/文档变更（yaml / json / md / 无业务逻辑的常量）→ 只派 QualityAgent + LogicAgent
- **ML/DL 代码**（路径含 layer / model / attn / flash_attn / train / inference）→ **必须加 PerformanceAgent**
- **鉴权/加密/API 接入**（路径含 auth / crypto / login / token / secret）→ **必须加 SecurityAgent**
- 通用业务逻辑 → 4 个 Agent 全派，small PR 上下文小，成本可以接受

**medium**（≤500 行 / ≤15 文件）：派全部 4 个 Agent，所有文件合并为一个批次。
正常功能迭代，一轮覆盖，关注是否有高风险 finding 需要第 2 轮追查。

**large**（≤1000 行 / ≤30 文件）：派全部 4 个 Agent，**按文件拆批并行**。
每个 Agent 任务不超过 5 个文件，超出时创建同类型的多个并行任务。
例如 20 个文件 → QualityAgent 分 4 个并行任务各检 5 个文件。

**xl**（>1000 行 或 >30 文件）：同 large 策略拆批，**额外在 reasoning 中注明"建议拆分 PR"**。
研究显示此规模缺陷检出率仅 28%，远低于小 PR 的 87%。

## 决策步骤（Chain of Thought）
1. **首轮**：先判断文件路径/类型，再根据规模分级决定派哪些 Agent 和如何分批
2. **后续轮**：评估是否有高风险 findings 需要追查；用 focus_hint 精确定向
3. **收益评估**：再派一轮能发现新问题的概率是否值得？充分则 FINISH

## 决策示例

**small PR（配置文件改了 3 行）**
```json
{
  "action": "DISPATCH",
  "reasoning": "small PR，纯配置变更，只需 Quality+Logic",
  "agents_to_dispatch": [
    {"agent_type": "QualityAgent", "files": ["config/settings.py"], "focus_hint": ""},
    {"agent_type": "LogicAgent",   "files": ["config/settings.py"], "focus_hint": ""}
  ]
}
```

**small PR（ML 热路径代码）**
```json
{
  "action": "DISPATCH",
  "reasoning": "small PR 但路径含 flash_attn，ML 热路径必须加 PerformanceAgent",
  "agents_to_dispatch": [
    {"agent_type": "QualityAgent",     "files": ["layers/flash_attn/attn.py"], "focus_hint": ""},
    {"agent_type": "LogicAgent",       "files": ["layers/flash_attn/attn.py"], "focus_hint": ""},
    {"agent_type": "PerformanceAgent", "files": ["layers/flash_attn/attn.py"], "focus_hint": "检查 forward 函数中是否有不必要的 dtype 转换（.float()）和 print 调试输出"}
  ]
}
```

**large PR（20 个文件，QualityAgent 拆批示例）**
```json
{
  "action": "DISPATCH",
  "reasoning": "full PR，20 文件拆为 4 批并行，全维度覆盖",
  "agents_to_dispatch": [
    {"agent_type": "SecurityAgent",    "files": ["auth/login.py", "db/query.py", ...所有文件], "focus_hint": ""},
    {"agent_type": "LogicAgent",       "files": ["auth/login.py", "db/query.py", ...所有文件], "focus_hint": ""},
    {"agent_type": "QualityAgent",     "files": ["src/a.py", "src/b.py", "src/c.py", "src/d.py", "src/e.py"], "focus_hint": ""},
    {"agent_type": "QualityAgent",     "files": ["src/f.py", "src/g.py", "src/h.py", "src/i.py", "src/j.py"], "focus_hint": ""},
    {"agent_type": "PerformanceAgent", "files": ["src/a.py", "src/b.py", ...所有文件], "focus_hint": ""}
  ]
}
```

**追查轮（SecurityAgent 发现 SQL 注入）**
```json
{
  "action": "DISPATCH",
  "reasoning": "SQL 注入点需追查参数传递路径",
  "agents_to_dispatch": [
    {"agent_type": "LogicAgent", "files": ["auth/login.py"], "focus_hint": "追查第 45 行 user_id 参数的来源：是直接来自 request 还是经过了校验？"}
  ]
}
```

## 输出格式
输出严格遵循以下 JSON，不要输出任何其他文字：
{
  "action": "DISPATCH 或 FINISH",
  "reasoning": "决策依据（中文，50字以内）",
  "agents_to_dispatch": []
}
DISPATCH 时 agents_to_dispatch 非空；FINISH 时为 []。
iteration >= 4 时必须输出 FINISH。
"""


def _severity_summary(findings: list[dict]) -> str:
    counts: dict[str, int] = {"CRITICAL": 0, "HIGH": 0, "MEDIUM": 0, "LOW": 0}
    for f in findings:
        counts[f.get("severity", "LOW")] += 1
    return " / ".join(f"{k}:{v}" for k, v in counts.items() if v)


async def run_supervisor(state: dict) -> dict:
    """返回 SupervisorDecision dict，包含 action / reasoning / agents_to_dispatch。"""
    iteration: int = state.get("iteration", 0)
    file_list: list[str] = state.get("file_list", [])
    findings: list[dict] = state.get("findings", [])
    reasonings: list[str] = state.get("supervisor_reasoning", [])
    pr_stats: dict = state.get("pr_stats", {})

    # 构建上下文
    findings_text = ""
    if findings:
        lines = []
        for f in findings[-20:]:
            lines.append(
                f"[{f.get('severity')}][{f.get('agent')}] {f.get('file')}:{f.get('line_start')} — {f.get('description', '')[:80]}"
            )
        findings_text = "\n".join(lines)
        findings_text = f"\n已有 {len(findings)} 条 findings（{_severity_summary(findings)}）:\n{findings_text}"
    else:
        findings_text = "\n暂无 findings（首轮检视）。"

    files_str = ", ".join(file_list[:20]) + ("..." if len(file_list) > 20 else "")
    history_str = (
        "\n".join(f"第{i+1}轮: {r}" for i, r in enumerate(reasonings))
        if reasonings else "（首轮，无历史）"
    )

    tier = pr_stats.get("tier", "lite")
    stats_str = (
        f"- PR 规模分级：**{tier}**\n"
        f"- 新增行数：{pr_stats.get('lines_added', '?')}，删除行数：{pr_stats.get('lines_removed', '?')}\n"
        f"- 可检视文件数：{pr_stats.get('files', len(file_list))}"
    )

    user_content = (
        f"## 当前状态\n"
        f"- 轮次：{iteration + 1}（最多 5 轮）\n"
        f"{stats_str}\n"
        f"- 变更文件：{files_str}\n"
        f"{findings_text}\n\n"
        f"## 历史决策推理\n{history_str}\n\n"
        "请根据 PR 规模分级策略输出本轮 SupervisorDecision JSON。"
    )

    llm = ChatOpenAI(
        model=settings.LLM_MODEL,
        base_url=settings.DASHSCOPE_BASE_URL,
        api_key=settings.DASHSCOPE_API_KEY,
        temperature=0,
    )

    try:
        resp = await llm.ainvoke([
            SystemMessage(content=_SYSTEM),
            HumanMessage(content=user_content),
        ])
        raw = resp.content.strip()

        # 提取 JSON
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            raw = m.group(0)
        decision = json.loads(raw)

        # iteration >= 4 强制 FINISH
        if iteration >= 4:
            decision["action"] = "FINISH"
            decision["reasoning"] = f"已达最大轮次（{iteration + 1}），强制结束"
            decision["agents_to_dispatch"] = []

        return decision

    except Exception as e:
        logger.error("Supervisor LLM failed (iteration=%d): %s", iteration, e)
        if iteration == 0:
            # 首轮失败时：派遣全部 Agent 兜底
            return {
                "action": "DISPATCH",
                "reasoning": f"Supervisor LLM 异常，首轮兜底全量派遣: {e}",
                "agents_to_dispatch": [
                    {"agent_type": t, "files": file_list, "focus_hint": ""}
                    for t in ["SecurityAgent", "LogicAgent", "QualityAgent", "PerformanceAgent"]
                ],
            }
        return {
            "action": "FINISH",
            "reasoning": f"Supervisor LLM 异常，强制结束: {e}",
            "agents_to_dispatch": [],
        }


_FOCUS_HINT_SYSTEM = (
    "你是 PR 检视协调者，根据文件类型为各专家 Agent 生成简短、精准的检视提示（中文，20字以内）。"
    "只输出 JSON，不要任何其他文字。"
)


async def get_focus_hints(
    file_list: list[str],
    languages: list[str],
    pr_stats: dict,
    base_tasks: list[dict],
) -> dict[str, str]:
    """LLM Advisor：为首轮各 Agent 生成 focus_hint（只生成 hints，不决策 Agent 选型）。

    比完整 Supervisor 调用成本低 80%+：prompt 更短，输出只需要 hints JSON。
    失败时返回空 dict，规则引擎已保证 Agent 派遣正确，hints 缺失只影响精准度不影响覆盖率。
    """
    agent_types = list({t["agent_type"] for t in base_tasks})
    if not agent_types:
        return {}

    files_str = "\n".join(f"- {f}" for f in file_list[:15])
    lang_str = ", ".join(languages) if languages else "未知"
    tier = pr_stats.get("tier", "medium")

    user_content = (
        f"PR 规模：{tier}（新增 {pr_stats.get('lines_added', 0)} 行）\n"
        f"编程语言：{lang_str}\n"
        f"变更文件：\n{files_str}\n\n"
        f"即将派遣的 Agent：{', '.join(agent_types)}\n\n"
        "请为每个 Agent 生成一条检视重点（中文，20字以内），格式示例：\n"
        '```json\n{"SecurityAgent": "关注 SQL 参数化查询", "LogicAgent": "检查空指针和边界"}\n```\n'
        "不需要派遣的 Agent 省略。只输出 JSON。"
    )

    llm = ChatOpenAI(
        model=settings.LLM_MODEL,
        base_url=settings.DASHSCOPE_BASE_URL,
        api_key=settings.DASHSCOPE_API_KEY,
        temperature=0,
    )
    try:
        resp = await llm.ainvoke([
            SystemMessage(content=_FOCUS_HINT_SYSTEM),
            HumanMessage(content=user_content),
        ])
        m = re.search(r"\{.*\}", (resp.content or "").strip(), re.DOTALL)
        if m:
            hints = json.loads(m.group(0))
            return {k: str(v) for k, v in hints.items() if k in agent_types and isinstance(v, str)}
    except Exception as e:
        logger.warning("get_focus_hints LLM call failed (non-critical): %s", e)
    return {}
