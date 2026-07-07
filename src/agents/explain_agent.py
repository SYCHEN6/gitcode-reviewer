"""ExplainAgent：解释指定代码片段的功能与实现逻辑（单次 LLM 调用）。"""

import json
import logging
import re

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from src.config import settings
from src.tools.gitcode_client import GitCodeClient

logger = logging.getLogger(__name__)

_SYSTEM = """你是代码解释专家。用户给出一段代码片段，请：
1. 简明扼要地解释这段代码的功能和作用
2. 指出关键实现细节（算法思路、数据结构选择、边界处理等）
3. 如有潜在注意事项（副作用、前置条件、性能特征）请简短提及

严格输出以下 JSON，不输出任何其他内容：
{
  "explanation": "功能解释（中文，200字以内）",
  "key_points": ["关键点1", "关键点2"]
}
key_points 最多 3 条，每条 40 字以内。"""


async def run_explain_agent(
    project_id: str,
    file_path: str,
    line_start: int,
    line_end: int,
    head_sha: str,
) -> dict:
    """获取文件内容，解释指定行范围的代码。

    返回 ExplainResponse：
    {
      "file": str,
      "line_start": int,
      "line_end": int,
      "explanation": str,
      "key_points": list[str],
    }
    """
    gc = GitCodeClient(settings.GITCODE_BASE_URL, settings.GITCODE_TOKEN)
    data = await gc.get_file_content(project_id, file_path, head_sha)
    content = data.get("content", "")

    lines = content.splitlines()
    s = max(0, line_start - 1)
    e = min(len(lines), (line_end if line_end > 0 else line_start + 29))
    actual_end = e
    snippet = "\n".join(lines[s:e])

    if not snippet.strip():
        return {
            "file": file_path,
            "line_start": line_start,
            "line_end": actual_end,
            "explanation": "指定行范围内无有效代码。",
            "key_points": [],
        }

    llm = ChatOpenAI(
        model=settings.LLM_MODEL,
        base_url=settings.DASHSCOPE_BASE_URL,
        api_key=settings.DASHSCOPE_API_KEY,
        temperature=0,
    )

    user_content = (
        f"文件：`{file_path}`（第 {line_start}–{actual_end} 行）\n\n"
        f"```\n{snippet[:3000]}\n```\n\n请解释这段代码。"
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
        result.setdefault("explanation", "")
        result.setdefault("key_points", [])
    except Exception as exc:
        logger.error("ExplainAgent LLM failed: %s", exc)
        result = {
            "explanation": f"代码解释生成失败：{exc}",
            "key_points": [],
        }

    return {
        "file": file_path,
        "line_start": line_start,
        "line_end": actual_end,
        **result,
    }
