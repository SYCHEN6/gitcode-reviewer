"""GitCode MCP Server — Streamable HTTP 协议（MCP 2025 规范）。

启动方式：
    python -m src.mcp.gitcode_server
"""

from mcp.server.fastmcp import FastMCP

from src.config import settings
from src.tools.gitcode_client import GitCodeClient, ProjectID

mcp = FastMCP(
    "gitcode-reviewer",
    host=settings.MCP_SERVER_HOST,
    port=settings.MCP_SERVER_PORT,
)
_client = GitCodeClient(settings.GITCODE_BASE_URL, settings.GITCODE_TOKEN)


# ── 读取工具（专家 Agent ReAct 循环调用）─────────────────────────────────────

@mcp.tool()
async def get_pr_diff(project_id: str, mr_iid: int) -> dict:
    """获取 MR 的完整 diff 和 SHA 信息。

    返回字段：
    - diff: 拼接后的 unified diff 文本
    - files: 变更文件路径列表
    - diffs: 每个文件的原始 diff 结构
    - base_sha / head_sha / start_sha: inline comment position 必要字段
    """
    return await _client.get_pr_diff(project_id, mr_iid)


@mcp.tool()
async def get_file_content(project_id: str, file_path: str, ref: str) -> dict:
    """获取指定 commit/branch 下的文件内容。

    返回字段：
    - content: 文件纯文本内容
    """
    return await _client.get_file_content(project_id, file_path, ref)


# ── 写入工具（仅 publish_node 调用）──────────────────────────────────────────

@mcp.tool()
async def post_inline_comment(
    project_id: str,
    mr_iid: int,
    body: str,
    position: dict,
) -> dict:
    """在 MR 指定行发送 inline comment。

    position 字段：
    - base_sha / start_sha / head_sha: 来自 get_pr_diff 返回值
    - new_path: 文件路径
    - new_line: 行号（对应 Finding.line_start）
    """
    return await _client.post_inline_comment(project_id, mr_iid, body, position)


@mcp.tool()
async def post_suggestion(
    project_id: str,
    mr_iid: int,
    suggestion_code: str,
    position: dict,
) -> dict:
    """在 MR 指定行发送 suggestion block（可一键应用的代码建议）。

    suggestion_code: 建议替换的新代码内容（不含 markdown 标记）
    position: 同 post_inline_comment
    """
    return await _client.post_suggestion(project_id, mr_iid, suggestion_code, position)


@mcp.tool()
async def post_mr_note(project_id: str, mr_iid: int, body: str) -> dict:
    """在 MR 发送全局评论（无 position，适用于摘要、错误提示等）。"""
    return await _client.post_mr_note(project_id, mr_iid, body)


@mcp.tool()
async def update_mr_description(project_id: str, mr_iid: int, body: str) -> dict:
    """更新 MR 描述（写入检视摘要和风险等级）。"""
    return await _client.update_mr_description(project_id, mr_iid, body)


@mcp.tool()
async def update_mr_label(project_id: str, mr_iid: int, labels: list[str]) -> dict:
    """更新 MR 标签（写入 ai-risk:low / ai-risk:high 等风险等级标签）。"""
    return await _client.update_mr_label(project_id, mr_iid, labels)


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
