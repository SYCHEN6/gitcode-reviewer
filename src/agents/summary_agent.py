"""SummaryAgent：单次 LLM 调用，生成 PR 变更摘要和风险评级。"""

import json
import logging
import re

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from src.config import settings

logger = logging.getLogger(__name__)

_SYSTEM = """你是 PR 代码检视摘要专家。根据专家 Agent 的 findings 和 PR 基础信息，
生成结构化的检视摘要。

输出严格遵循以下 JSON 格式，不要输出任何其他文字：
{
  "total_files": 变更文件数（整数）,
  "total_lines": 估算变更行数（整数）,
  "impact_analysis": "改动范围与影响面分析（中文，100字以内）",
  "risk_level": "LOW|MEDIUM|HIGH|CRITICAL",
  "risk_reason": "风险等级主因（中文，50字以内）",
  "focus_points": ["关注点1", "关注点2"]（最多 5 条）
}

风险评级参考：
- CRITICAL：存在 CRITICAL severity findings，或涉及认证/支付核心逻辑大改动
- HIGH：存在 HIGH findings，或改动范围超过 10 个文件涉及核心模块
- MEDIUM：存在 MEDIUM findings，或有一定规模的业务逻辑修改
- LOW：仅有 LOW findings 或无问题，改动范围小
"""


async def run_summary_agent(
    file_list: list[str],
    raw_diff: str,
    findings: list[dict],
) -> dict:
    """返回 SummaryOutput dict。"""
    total_files = len(file_list)
    total_lines = raw_diff.count("\n") if raw_diff else 0

    sev_counts: dict[str, int] = {"CRITICAL": 0, "HIGH": 0, "MEDIUM": 0, "LOW": 0}
    for f in findings:
        sev_counts[f.get("severity", "LOW")] += 1

    findings_text = ""
    if findings:
        lines = [
            f"[{f.get('severity')}][{f.get('agent')}] {f.get('file')}:{f.get('line_start')} — {f.get('description', '')[:100]}"
            for f in findings[:30]
        ]
        findings_text = "\n".join(lines)
        if len(findings) > 30:
            findings_text += f"\n...（共 {len(findings)} 条，仅展示前 30 条）"
    else:
        findings_text = "无 findings。"

    user_content = f"""## PR 基础信息
- 变更文件数：{total_files}
- 变更行数（估算）：{total_lines}
- 变更文件：{', '.join(file_list[:20])}{'...' if total_files > 20 else ''}

## Findings 汇总
严重程度分布：CRITICAL={sev_counts['CRITICAL']} HIGH={sev_counts['HIGH']} MEDIUM={sev_counts['MEDIUM']} LOW={sev_counts['LOW']}

{findings_text}

请输出 SummaryOutput JSON。"""

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
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            raw = m.group(0)
        result = json.loads(raw)
        result.setdefault("total_files", total_files)
        result.setdefault("total_lines", total_lines)
        return result
    except Exception as e:
        logger.error("SummaryAgent LLM failed: %s", e)
        return {
            "total_files":     total_files,
            "total_lines":     total_lines,
            "impact_analysis": f"AI 摘要生成失败，请人工确认（错误：{e}）",
            "risk_level":      "HIGH" if sev_counts["CRITICAL"] + sev_counts["HIGH"] > 0 else "MEDIUM",
            "risk_reason":     "摘要生成异常，基于 findings 自动评级",
            "focus_points":    [f"共发现 {len(findings)} 个问题"],
        }
