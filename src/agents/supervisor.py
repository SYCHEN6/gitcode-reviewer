"""Supervisor Agent：每轮读取完整 State，用 LLM 决策下一步行动。"""

import json
import logging
import re

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from src.config import settings

logger = logging.getLogger(__name__)

_SYSTEM = """你是 PR 代码检视的协调者（Supervisor）。每轮你会收到 PR 的完整信息和已有的检视结果，
然后决定是否继续派遣专家 Agent，或者结束检视进入汇总阶段。

可用的专家 Agent：
- SecurityAgent：检测安全漏洞（SQL 注入 / 硬编码密钥 / XSS / SSRF 等）
- LogicAgent：检测逻辑缺陷（空指针 / 资源泄露 / 竞态 / 边界条件等）
- QualityAgent：检测代码质量（可读性 / 命名 / 重复代码 / 过长函数等）
- PerformanceAgent：检测性能问题（N+1 / 内存 / 同步 I/O 等）

输出严格遵循以下 JSON 格式，不要输出任何其他文字：
{
  "action": "DISPATCH",
  "reasoning": "本轮决策依据（中文，50字以内）",
  "agents_to_dispatch": [
    {
      "agent_type": "SecurityAgent",
      "files": ["文件路径"],
      "focus_hint": "专项提示（首轮为空字符串）"
    }
  ]
}

或（结束时）：
{
  "action": "FINISH",
  "reasoning": "结束原因（中文，50字以内）",
  "agents_to_dispatch": []
}

策略：
- 第 1 轮：通常同时派遣全部 4 个专家 Agent，各自分析所有变更文件
- 后续轮次：仅当上一轮 findings 揭示了需要深入追查的线索时才继续派遣，否则输出 FINISH
- iteration >= 4 时必须输出 FINISH
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

    # 构建上下文
    findings_text = ""
    if findings:
        lines = []
        for f in findings[-20:]:  # 最多展示最近 20 条
            lines.append(
                f"[{f.get('severity')}][{f.get('agent')}] {f.get('file')}:{f.get('line_start')} — {f.get('description', '')[:80]}"
            )
        findings_text = "\n".join(lines)
        findings_text = f"\n已有 {len(findings)} 条 findings（{_severity_summary(findings)}）:\n{findings_text}"
    else:
        findings_text = "\n暂无 findings（首轮检视）。"

    user_content = f"""## 当前状态
- 轮次：{iteration + 1}（最多 5 轮）
- 变更文件数：{len(file_list)}
- 变更文件：{', '.join(file_list[:15])}{'...' if len(file_list) > 15 else ''}
{findings_text}

## 历史决策推理
{chr(10).join(f'第{i+1}轮: {r}' for i, r in enumerate(reasonings)) if reasonings else '（首轮，无历史）'}

请输出本轮 SupervisorDecision JSON。"""

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
