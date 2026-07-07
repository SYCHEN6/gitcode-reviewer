"""Webhook 事件处理器（合并请求 / 评论 / push）。"""

import logging
import re
import time

import redis.asyncio as aioredis
from fastapi import BackgroundTasks

from src.graph.review_graph import run_review_graph, run_summary_only

logger = logging.getLogger(__name__)

_REDIS_TTL = 86400  # 24h
# 用于标记"AI 解释已追加"的 HTML 注释，防止 webhook 重复触发
# 选用含随机后缀的字符串，降低 LLM 在输出中自行生成该 marker 的概率
_EXPLAIN_MARKER = "<!-- __AI_EXPLAIN_APPENDED_7f3a__ -->"

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

    elif note_body.startswith("/ai explain") and _EXPLAIN_MARKER not in note_body:
        note_id: int = attrs.get("id", 0)
        # 幂等保护：同一 note_id 只处理一次（防止 webhook 重复投递）
        if note_id > 0:
            explain_key = f"explain:{project_id}:{mr_iid}:{note_id}"
            if await redis.exists(explain_key):
                logger.info("Skipping duplicate explain: %s", explain_key)
                return
            await redis.setex(explain_key, _REDIS_TTL, "running")
        _handle_explain(note_body, project_id, mr_iid, background_tasks, note_id)

    elif note_body == "/ai help":
        background_tasks.add_task(_post_help, project_id, mr_iid)
        logger.info("/ai help triggered for project=%s mr=%s", project_id, mr_iid)

    else:
        logger.info("handle_note: not a command, note_body=%r", note_body)


def _handle_explain(
    note_body: str,
    project_id: str,
    mr_iid: int,
    background_tasks: BackgroundTasks,
    note_id: int = 0,
) -> None:
    """解析 /ai explain 命令，支持两种用法：

    1. /ai explain <file>:<line>[-<end>]  — 按文件行号拉取
    2. /ai explain\\n<code snippet>        — 直接粘贴代码片段
    """
    # 提取 /ai explain 后面的内容
    rest = re.sub(r"^/ai\s+explain\s*", "", note_body, count=1, flags=re.IGNORECASE).strip()

    # 优先尝试 file:line 格式
    m = re.match(r"^([^\s:]+):(\d+)(?:-(\d+))?$", rest)
    if m:
        file_path  = m.group(1)
        line_start = int(m.group(2))
        line_end   = int(m.group(3)) if m.group(3) else 0
        logger.info(
            "/ai explain [file]: project=%s mr=%s file=%s line=%s-%s",
            project_id, mr_iid, file_path, line_start, line_end or line_start,
        )
        background_tasks.add_task(
            _run_explain_and_post,
            project_id, mr_iid, file_path, line_start, line_end, note_id,
        )
        return

    # 剩余内容当做代码片段（去除 Markdown 代码块围栏）
    snippet = re.sub(r"^```\w*\n?", "", rest)
    snippet = re.sub(r"\n?```$", "", snippet).strip()

    if not snippet:
        background_tasks.add_task(
            _post_explain_error,
            project_id, mr_iid,
            "请在 `/ai explain` 后粘贴需要解释的代码，或使用 `/ai explain <文件路径>:<行号>` 格式。",
        )
        return

    logger.info("/ai explain [snippet]: project=%s mr=%s snippet_len=%d", project_id, mr_iid, len(snippet))
    background_tasks.add_task(_run_explain_snippet_and_post, project_id, mr_iid, snippet, note_id, rest)


async def _run_explain_and_post(
    project_id: str,
    mr_iid: int,
    file_path: str,
    line_start: int,
    line_end: int,
    note_id: int = 0,
) -> None:
    """调用 ExplainAgent 并将结果追加到原始评论（或发布为新评论）。"""
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

    explain_lines = [
        f"## 💡 代码解释 — `{file_path}:{line_start}`",
        "",
        explanation,
    ]
    if key_points:
        explain_lines += ["", "**关键点：**"]
        explain_lines += [f"- {p}" for p in key_points]
    explain_body = "\n".join(explain_lines).replace(_EXPLAIN_MARKER, "")

    # 优先编辑原始评论（追加解释），避免产生两条独立评论
    if note_id > 0:
        try:
            orig = await gc.get_pr_comment(project_id, note_id)
            orig_body = orig.get("body", "")
            if _EXPLAIN_MARKER in orig_body:
                logger.info("explain: marker already present, skipping note_id=%s", note_id)
                return
            new_body = f"{orig_body}\n\n{_EXPLAIN_MARKER}\n\n{explain_body}"
            await gc.update_pr_comment(project_id, mr_iid, note_id, new_body)
            logger.info("explain appended to original comment: project=%s mr=%s note_id=%s file=%s:%s",
                        project_id, mr_iid, note_id, file_path, line_start)
            return
        except Exception as e:
            logger.warning("_run_explain_and_post: edit original comment failed, fallback: %s", e)

    # Fallback：发布新评论（带代码位置引用）
    fallback_lines = [
        f"**你提问的代码：** `{file_path}` 第 {line_start}–{actual_end} 行",
        "",
        "---",
        "",
        explain_body,
    ]
    try:
        await gc.post_mr_note(project_id, mr_iid, "\n".join(fallback_lines))
        logger.info("explain comment posted (fallback): project=%s mr=%s file=%s:%s",
                    project_id, mr_iid, file_path, line_start)
    except Exception as e:
        logger.error("_run_explain_and_post: post_mr_note failed: %s", e)


async def _run_explain_snippet_and_post(
    project_id: str,
    mr_iid: int,
    snippet: str,
    note_id: int = 0,
    raw_user_input: str = "",
) -> None:
    """调用 ExplainAgent（片段模式）并将结果追加到原始评论（或发布为新评论）。"""
    from src.agents.explain_agent import run_explain_agent_snippet
    from src.tools.gitcode_client import GitCodeClient
    from src.config import settings

    try:
        result = await run_explain_agent_snippet(snippet)
    except Exception as e:
        logger.error("_run_explain_snippet_and_post: explain_agent failed: %s", e)
        await _post_explain_error(project_id, mr_iid, f"代码解释失败：{e}")
        return

    explanation = result.get("explanation", "")
    key_points  = result.get("key_points", [])

    explain_lines = ["## 💡 代码解释", "", explanation]
    if key_points:
        explain_lines += ["", "**关键点：**"]
        explain_lines += [f"- {p}" for p in key_points]
    explain_body = "\n".join(explain_lines).replace(_EXPLAIN_MARKER, "")

    gc = GitCodeClient(settings.GITCODE_BASE_URL, settings.GITCODE_TOKEN)

    # 优先编辑原始评论（追加解释），避免产生两条独立评论
    if note_id > 0:
        try:
            orig = await gc.get_pr_comment(project_id, note_id)
            orig_body = orig.get("body", "")
            # 如果 marker 已存在（并发重入保护），直接跳过
            if _EXPLAIN_MARKER in orig_body:
                logger.info("snippet explain: marker already present, skipping note_id=%s", note_id)
                return
            new_body = f"{orig_body}\n\n{_EXPLAIN_MARKER}\n\n{explain_body}"
            await gc.update_pr_comment(project_id, mr_iid, note_id, new_body)
            logger.info("snippet explain appended to original comment: project=%s mr=%s note_id=%s",
                        project_id, mr_iid, note_id)
            return
        except Exception as e:
            logger.warning("_run_explain_snippet_and_post: edit original comment failed, fallback: %s", e)

    # Fallback：发布新评论（带代码引用块）
    display_code = raw_user_input or snippet
    fallback_lines: list[str] = []
    if display_code.strip():
        fallback_lines += ["**你提问的代码：**", ""]
        fallback_lines += [f"> {ln}" for ln in display_code.splitlines()]
        fallback_lines += ["", "---", ""]
    fallback_lines.append(explain_body)

    try:
        await gc.post_mr_note(project_id, mr_iid, "\n".join(fallback_lines))
        logger.info("snippet explain comment posted (fallback): project=%s mr=%s", project_id, mr_iid)
    except Exception as e:
        logger.error("_run_explain_snippet_and_post: post_mr_note failed: %s", e)


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

        # 先查出哪些 MR 有 pending suggestion 涉及这批文件（apply 前查，apply 后这些行已不是 pending）
        mr_ids = await repository.get_open_mr_ids(project_id, file_paths)

        updated = await repository.mark_suggestions_applied(project_id, file_paths)
        if updated:
            logger.info(
                "suggestion_status: marked %d suggestion(s) as applied for %s",
                updated, project_id,
            )

        # apply 后重算各 MR 的风险标签
        if mr_ids:
            from src.tools.gitcode_client import GitCodeClient
            from src.config import settings
            gc = GitCodeClient(settings.GITCODE_BASE_URL, settings.GITCODE_TOKEN)
            for mr_iid in mr_ids:
                try:
                    open_high = await repository.count_open_critical_high(project_id, mr_iid)
                    label = "ai-risk-high" if open_high > 0 else "ai-risk-low"
                    await gc.update_mr_label(project_id, mr_iid, [label])
                    logger.info(
                        "Risk label recalculated: %s mr=%s → %s (open_critical_high=%d)",
                        project_id, mr_iid, label, open_high,
                    )
                except Exception as e:
                    logger.warning("Risk label recalc failed for mr=%s: %s", mr_iid, e)
    except Exception as e:
        logger.debug("mark_suggestions_applied skipped (DB unavailable): %s", e)
