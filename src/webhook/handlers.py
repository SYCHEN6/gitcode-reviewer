"""Webhook 事件处理器（合并请求 / 评论 / push）。"""

import logging
import re
import time

import redis.asyncio as aioredis
from fastapi import BackgroundTasks

from src.graph.review_graph import run_review_graph, run_summary_only

logger = logging.getLogger(__name__)

_REDIS_TTL = 86400  # 24h

_HELP_TEXT = """\
## 🤖 AI Code Reviewer — 可用命令

| 命令 | 说明 |
|------|------|
| `/ai review` | 重新触发完整多 Agent 代码检视 |
| `/ai summary` | 仅生成 PR 变更摘要（不运行专家 Agent，速度更快） |
| `/ai explain <file>:<line>[-<end>]` | 解释指定代码行的功能，如 `/ai explain src/foo.py:42` 或 `/ai explain src/foo.py:42-60` |
| `/ai help` | 显示此帮助信息 |

> 提示：`/ai review` 和 `/ai summary` 会更新 MR 评论区的"AI 检视报告"；`/ai explain` 会在当前评论下发布代码解释。
"""


def _project_id(payload: dict) -> str:
    """从 Webhook payload 提取 'owner/repo' 格式的项目标识。"""
    return payload["project"]["path_with_namespace"]


async def handle_merge_request(
    payload: dict,
    background_tasks: BackgroundTasks,
    redis: aioredis.Redis,
) -> None:
    attrs = payload.get("object_attributes", {})
    project_id = _project_id(payload)
    mr_iid: int = attrs["iid"]
    commit_sha: str = attrs.get("last_commit", {}).get("id", "unknown")

    redis_key = f"review:{project_id}:{mr_iid}:{commit_sha}"
    if await redis.exists(redis_key):
        logger.info("Skipping duplicate review: %s", redis_key)
        return

    await redis.setex(redis_key, _REDIS_TTL, "running")
    background_tasks.add_task(run_review_graph, project_id, mr_iid, commit_sha)
    logger.info("Queued review for project=%s mr=%s sha=%s", project_id, mr_iid, commit_sha)


async def handle_note(
    payload: dict,
    background_tasks: BackgroundTasks,
    redis: aioredis.Redis,
) -> None:
    attrs = payload.get("object_attributes", {})
    noteable_type = attrs.get("noteable_type", "")
    note_body: str = attrs.get("note", "").strip()
    logger.info("handle_note: noteable_type=%r note_body=%r", noteable_type, note_body[:80])

    if noteable_type != "MergeRequest":
        logger.info("handle_note: skip, noteable_type=%r is not MergeRequest", noteable_type)
        return

    project_id = _project_id(payload)
    mr_iid: int = payload.get("merge_request", {}).get("iid", 0)
    logger.info("handle_note: project=%s mr_iid=%s", project_id, mr_iid)

    if not mr_iid:
        logger.warning("handle_note: mr_iid not found in payload, keys=%s", list(payload.keys()))
        return

    # ── 命令路由 ──────────────────────────────────────────────────────────────

    if note_body == "/ai review":
        commit_sha = f"cmd:{int(time.time())}"
        redis_key = f"review:{project_id}:{mr_iid}:{commit_sha}"
        await redis.setex(redis_key, _REDIS_TTL, "running")
        background_tasks.add_task(run_review_graph, project_id, mr_iid, commit_sha)
        logger.info("/ai review triggered for project=%s mr=%s", project_id, mr_iid)

    elif note_body == "/ai summary":
        background_tasks.add_task(run_summary_only, project_id, mr_iid)
        logger.info("/ai summary triggered for project=%s mr=%s", project_id, mr_iid)

    elif note_body.startswith("/ai explain"):
        _handle_explain(note_body, project_id, mr_iid, payload, background_tasks)

    elif note_body == "/ai help":
        background_tasks.add_task(_post_help, project_id, mr_iid)
        logger.info("/ai help triggered for project=%s mr=%s", project_id, mr_iid)

    else:
        logger.info("handle_note: not a command, note_body=%r", note_body)


def _handle_explain(
    note_body: str,
    project_id: str,
    mr_iid: int,
    payload: dict,
    background_tasks: BackgroundTasks,
) -> None:
    """解析 /ai explain <file>:<line>[-<end>] 并派发任务。"""
    # 匹配格式：/ai explain src/foo.py:42  或  /ai explain src/foo.py:42-60
    m = re.search(r"/ai\s+explain\s+([^\s:]+):(\d+)(?:-(\d+))?", note_body)
    if not m:
        logger.warning("/ai explain: 格式错误，期望 /ai explain <file>:<line>[-<end>]，实际=%r", note_body)
        background_tasks.add_task(
            _post_explain_error,
            project_id, mr_iid,
            "命令格式错误，请使用：`/ai explain <文件路径>:<行号>` 例如 `/ai explain src/foo.py:42`",
        )
        return

    file_path  = m.group(1)
    line_start = int(m.group(2))
    line_end   = int(m.group(3)) if m.group(3) else 0

    logger.info(
        "/ai explain: project=%s mr=%s file=%s line=%s-%s",
        project_id, mr_iid, file_path, line_start, line_end or line_start,
    )
    background_tasks.add_task(
        _run_explain_and_post,
        project_id, mr_iid, file_path, line_start, line_end,
    )


async def _run_explain_and_post(
    project_id: str,
    mr_iid: int,
    file_path: str,
    line_start: int,
    line_end: int,
) -> None:
    """调用 ExplainAgent 并将结果发布为 MR 评论。"""
    from src.agents.explain_agent import run_explain_agent
    from src.tools.gitcode_client import GitCodeClient
    from src.config import settings

    gc = GitCodeClient(settings.GITCODE_BASE_URL, settings.GITCODE_TOKEN)
    try:
        diff_data = await gc.get_pr_diff(project_id, mr_iid)
        head_sha  = diff_data.get("head_sha", "")
    except Exception as e:
        logger.error("_run_explain_and_post: get_pr_diff failed: %s", e)
        return

    try:
        result = await run_explain_agent(project_id, file_path, line_start, line_end, head_sha)
    except Exception as e:
        logger.error("_run_explain_and_post: explain_agent failed: %s", e)
        await _post_explain_error(project_id, mr_iid, f"代码解释失败：{e}")
        return

    explanation = result.get("explanation", "")
    key_points  = result.get("key_points", [])
    actual_end  = result.get("line_end", line_end or line_start)

    lines = [
        f"## 💡 代码解释 — `{file_path}:{line_start}`",
        "",
        explanation,
    ]
    if key_points:
        lines += ["", "**关键点：**"]
        lines += [f"- {p}" for p in key_points]
    lines += ["", f"*`{file_path}` 第 {line_start}–{actual_end} 行*"]

    body = "\n".join(lines)
    try:
        await gc.post_mr_note(project_id, mr_iid, body)
        logger.info("explain comment posted: project=%s mr=%s file=%s:%s", project_id, mr_iid, file_path, line_start)
    except Exception as e:
        logger.error("_run_explain_and_post: post_mr_note failed: %s", e)


async def _post_explain_error(project_id: str, mr_iid: int, message: str) -> None:
    from src.tools.gitcode_client import GitCodeClient
    from src.config import settings
    gc = GitCodeClient(settings.GITCODE_BASE_URL, settings.GITCODE_TOKEN)
    try:
        await gc.post_mr_note(project_id, mr_iid, f"> ⚠️ {message}")
    except Exception as e:
        logger.error("_post_explain_error: %s", e)


async def _post_help(project_id: str, mr_iid: int) -> None:
    from src.tools.gitcode_client import GitCodeClient
    from src.config import settings
    gc = GitCodeClient(settings.GITCODE_BASE_URL, settings.GITCODE_TOKEN)
    try:
        await gc.post_mr_note(project_id, mr_iid, _HELP_TEXT)
    except Exception as e:
        logger.error("_post_help failed: %s", e)


async def handle_push(payload: dict, background_tasks: BackgroundTasks) -> None:
    """检测 suggestion apply commit，更新 suggestion_status。"""
    project_id = payload.get("project", {}).get("path_with_namespace", "")

    for commit in payload.get("commits", []):
        msg: str = commit.get("message", "")
        if not re.search(r"Apply \d* ?suggestion", msg, re.IGNORECASE):
            continue

        commit_id = commit.get("id", "")[:8]
        logger.info("Detected suggestion-apply commit: %s project=%s", commit_id, project_id)

        # 收集本次 commit 变更的文件，匹配待更新的 suggestions
        changed_files: list[str] = (
            commit.get("modified", [])
            + commit.get("added", [])
        )
        if project_id and changed_files:
            background_tasks.add_task(
                _mark_suggestions_applied, project_id, changed_files,
            )


async def _mark_suggestions_applied(project_id: str, file_paths: list[str]) -> None:
    try:
        from src.db import repository
        updated = await repository.mark_suggestions_applied(project_id, file_paths)
        if updated:
            logger.info(
                "suggestion_status: marked %d suggestion(s) as applied for %s",
                updated, project_id,
            )
    except Exception as e:
        logger.debug("mark_suggestions_applied skipped (DB unavailable): %s", e)
