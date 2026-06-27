"""Phase 1 单 Agent 检视器 — 调用 MCP 工具完成端到端检视。

工作流程：
1. 通过 MCP 工具拉取 MR diff（get_pr_diff）
2. 调用 LLM（qwen-max via DashScope）分析 diff
3. 通过 MCP 工具将摘要评论回写到 MR（post_mr_note）
"""

import json
import logging
from contextlib import asynccontextmanager

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from src.config import settings

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
你是一个专业的代码检视 Agent。请阅读下面的 MR diff，给出简洁的中文检视意见：
1. 主要变更摘要（1-2 句）
2. 潜在风险或值得关注的问题（如果有）
3. 总体评价

格式要求：直接输出 markdown，不要额外解释。
"""


@asynccontextmanager
async def _mcp_session():
    url = f"http://{settings.MCP_SERVER_HOST}:{settings.MCP_SERVER_PORT}/mcp"
    async with streamablehttp_client(url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


async def _call_tool(session: ClientSession, name: str, args: dict) -> dict:
    result = await session.call_tool(name, args)
    if result.content:
        text = result.content[0].text
        try:
            return json.loads(text)
        except Exception:
            return {"raw": text}
    return {}


async def run_simple_review(project_id: int, mr_iid: int, commit_sha: str) -> None:
    logger.info("Simple review start: project=%s mr=%s", project_id, mr_iid)
    try:
        async with _mcp_session() as session:
            diff_result = await _call_tool(session, "get_pr_diff", {
                "project_id": project_id,
                "mr_iid": mr_iid,
            })

        diff_text: str = diff_result.get("diff", "")
        if not diff_text:
            logger.warning("Empty diff for project=%s mr=%s", project_id, mr_iid)
            return

        # 截断超长 diff 避免超出 token 限制
        if len(diff_text) > 12000:
            diff_text = diff_text[:12000] + "\n\n[...diff truncated...]"

        llm = ChatOpenAI(
            model=settings.LLM_MODEL,
            api_key=settings.DASHSCOPE_API_KEY,
            base_url=settings.DASHSCOPE_BASE_URL,
        )
        messages = [
            SystemMessage(content=_SYSTEM_PROMPT),
            HumanMessage(content=f"## MR Diff\n\n```diff\n{diff_text}\n```"),
        ]
        response = await llm.ainvoke(messages)
        review_body = response.content

        async with _mcp_session() as session:
            await _call_tool(session, "post_mr_note", {
                "project_id": project_id,
                "mr_iid": mr_iid,
                "body": f"## 🤖 AI 代码检视\n\n{review_body}",
            })

        logger.info("Simple review done: project=%s mr=%s", project_id, mr_iid)
    except Exception:
        logger.exception("Simple review failed: project=%s mr=%s", project_id, mr_iid)
