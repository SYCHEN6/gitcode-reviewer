"""专家 Agent 公共实现：文件读取 + LLM 分析 + Finding 解析。"""

import json
import logging
import re
import uuid

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from src.config import settings
from src.tools.gitcode_client import GitCodeClient

logger = logging.getLogger(__name__)

_FINDING_SCHEMA_HINT = """
每个 finding 必须是合法 JSON 对象，字段如下：
{
  "file": "文件路径",
  "line_start": 起始行号（整数）,
  "line_end": 结束行号（整数，可与 line_start 相同）,
  "severity": "CRITICAL|HIGH|MEDIUM|LOW",
  "description": "问题描述（中文，面向开发者，100字以内）",
  "suggestion_code": "建议替换的完整代码块（无问题则为空字符串）"
}
无任何问题时直接返回 []。
"""


def _make_llm() -> ChatOpenAI:
    return ChatOpenAI(
        model=settings.LLM_MODEL,
        base_url=settings.DASHSCOPE_BASE_URL,
        api_key=settings.DASHSCOPE_API_KEY,
        temperature=0,
    )


def _parse_findings(raw: str, agent_type: str) -> list[dict]:
    """从 LLM 输出中提取 Finding JSON 数组，容错处理各种格式。"""
    text = raw.strip()

    # 尝试提取 ```json ... ``` 或 ``` ... ``` 代码块
    m = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.DOTALL)
    if m:
        text = m.group(1)
    else:
        # 直接找第一个 [ ... ]
        m = re.search(r"\[.*\]", text, re.DOTALL)
        if m:
            text = m.group(0)

    try:
        items = json.loads(text)
    except Exception:
        logger.warning("[%s] Failed to parse findings JSON: %s", agent_type, raw[:200])
        return []

    if not isinstance(items, list):
        return []

    result = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if not item.get("file") or not item.get("description"):
            continue
        result.append({
            "finding_id":      str(uuid.uuid4()),
            "agent":           agent_type,
            "severity":        item.get("severity", "LOW"),
            "category":        _category_of(agent_type),
            "file":            item.get("file", ""),
            "line_start":      int(item.get("line_start", 0)),
            "line_end":        int(item.get("line_end", item.get("line_start", 0))),
            "diff_position":   0,  # 由 publish_node 计算
            "description":     item.get("description", ""),
            "suggestion_code": item.get("suggestion_code", ""),
            "norm_reference":  item.get("norm_reference", ""),
        })
    return result


def _category_of(agent_type: str) -> str:
    return {
        "SecurityAgent":    "security",
        "LogicAgent":       "logic",
        "QualityAgent":     "quality",
        "PerformanceAgent": "performance",
    }.get(agent_type, "quality")


async def run_expert_agent(
    agent_type: str,
    system_prompt: str,
    task: dict,
    head_sha: str,
) -> list[dict]:
    """
    通用专家 Agent 实现。
    task 字段：agent_type, files, focus_hint, diff_slice, project_id, mr_iid
    """
    project_id: str = task["project_id"]
    files: list[str] = task.get("files", [])
    focus_hint: str = task.get("focus_hint", "")
    diff_slice: str = task.get("diff_slice", "")

    gc = GitCodeClient(settings.GITCODE_BASE_URL, settings.GITCODE_TOKEN)

    # 读取最多 8 个文件内容
    file_sections: list[str] = []
    for fpath in files[:8]:
        try:
            data = await gc.get_file_content(project_id, fpath, head_sha)
            content = data.get("content", "")[:4000]
            file_sections.append(f"### {fpath}\n```\n{content}\n```")
        except Exception as e:
            logger.warning("[%s] Cannot fetch %s: %s", agent_type, fpath, e)
            file_sections.append(f"### {fpath}\n[文件获取失败: {e}]")

    files_text = "\n\n".join(file_sections)

    user_content = f"""## Diff 变更片段
{diff_slice[:3000] if diff_slice else "（无 diff 片段）"}

## 变更文件内容
{files_text}

## 专项提示
{focus_hint if focus_hint else "无，请全面检查上述文件的变更内容。"}

## 输出要求
{_FINDING_SCHEMA_HINT}
"""

    llm = _make_llm()
    try:
        response = await llm.ainvoke([
            SystemMessage(content=system_prompt),
            HumanMessage(content=user_content),
        ])
        return _parse_findings(response.content, agent_type)
    except Exception as e:
        logger.error("[%s] LLM call failed: %s", agent_type, e)
        return []
