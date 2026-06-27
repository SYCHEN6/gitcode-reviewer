"""Webhook 事件处理器（合并请求 / 评论 / push）。"""

import logging
import re
import time

import redis.asyncio as aioredis
from fastapi import BackgroundTasks

from src.agents.simple_reviewer import run_simple_review

logger = logging.getLogger(__name__)

_REDIS_TTL = 86400  # 24h


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
    background_tasks.add_task(run_simple_review, project_id, mr_iid, commit_sha)
    logger.info("Queued review for project=%s mr=%s sha=%s", project_id, mr_iid, commit_sha)


async def handle_note(
    payload: dict,
    background_tasks: BackgroundTasks,
    redis: aioredis.Redis,
) -> None:
    attrs = payload.get("object_attributes", {})
    # 只处理 MR 上的评论，忽略 Issue 评论
    if attrs.get("noteable_type") != "MergeRequest":
        return

    note_body: str = attrs.get("note", "").strip()
    project_id = _project_id(payload)
    mr_iid: int = payload.get("merge_request", {}).get("iid", 0)

    if not mr_iid:
        return

    if note_body == "/ai review":
        # /ai review 每次强制执行，用 timestamp 后缀绕过幂等
        commit_sha = f"cmd:{int(time.time())}"
        redis_key = f"review:{project_id}:{mr_iid}:{commit_sha}"
        await redis.setex(redis_key, _REDIS_TTL, "running")
        background_tasks.add_task(run_simple_review, project_id, mr_iid, commit_sha)
        logger.info("/ai review triggered for project=%s mr=%s", project_id, mr_iid)
    else:
        logger.info("Ignored note command: %r", note_body)


async def handle_push(payload: dict, background_tasks: BackgroundTasks) -> None:
    """检测 suggestion apply commit，预留给 Phase 3 实现。"""
    for commit in payload.get("commits", []):
        msg: str = commit.get("message", "")
        if re.search(r"Apply \d* ?suggestion", msg, re.IGNORECASE):
            logger.info("Detected suggestion-apply commit: %s", commit.get("id"))
            # Phase 3 will handle suggestion status update here
