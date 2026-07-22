"""专家 Agent 集中调度节点：并行运行所有派遣的 Agent，结果聚合写入 State。"""
import asyncio
import logging
from collections import Counter

from src.graph.diff_utils import detect_new_files, extract_diff_slice
from src.graph.dispatch import AGENT_MAP
from src.graph.state import ReviewState

logger = logging.getLogger(__name__)


async def run_agents_node(state: ReviewState) -> dict:
    tasks = state.get("agents_to_dispatch", [])
    head_sha = state.get("head_sha", "")
    diffs = state.get("diffs", [])
    project_id = state["project_id"]
    mr_iid = state["mr_iid"]
    languages = state.get("languages", [])
    task_id = state.get("task_id", "")
    iteration = state.get("iteration", 0)

    # ── Step Checkpoint：首轮加载已完成 Agent 的结果 ─────────────────────────
    checkpoint: dict[str, list[dict]] = {}
    if task_id and iteration == 0:
        try:
            from src.db.repository import load_agent_findings
            checkpoint = await load_agent_findings(task_id)
            if checkpoint:
                logger.info(
                    "Checkpoint resume: %d agent(s) already done: %s",
                    len(checkpoint), list(checkpoint.keys()),
                )
        except Exception as e:
            logger.warning("load_agent_findings failed (checkpoint skipped): %s", e)

    agent_batch_counts: Counter = Counter(t.get("agent_type", "") for t in tasks)
    reviewable_names = {d.get("filename", "") for d in diffs}

    # 跨 Agent 共享文件内容缓存
    file_cache: dict[str, str] = {}

    async def _run_one(task: dict) -> list[dict]:
        agent_type = task.get("agent_type", "")
        is_single_batch = agent_batch_counts[agent_type] == 1

        if is_single_batch and agent_type in checkpoint:
            cached = checkpoint[agent_type]
            logger.info(
                "[%s] checkpoint hit: skipping LLM, using %d cached findings",
                agent_type, len(cached),
            )
            return cached

        agent_fn = AGENT_MAP.get(agent_type)
        if not agent_fn:
            logger.warning("Unknown agent_type: %s", agent_type)
            return []

        files = task.get("files", [])
        new_files = detect_new_files(diffs, set(files) & reviewable_names)
        token_stats: dict = {}
        full_task = {
            **task,
            "project_id":  project_id,
            "mr_iid":      mr_iid,
            "diff_slice":  extract_diff_slice(diffs, files),
            "new_files":   new_files,
            "languages":   languages,
            "_file_cache": file_cache,
            "_token_stats": token_stats,
        }

        start_ms = int(asyncio.get_event_loop().time() * 1000)
        findings: list[dict] = []
        agent_status = "completed"
        try:
            kwargs: dict = {}
            if task.get("model"):
                kwargs["model"] = task["model"]
            if task.get("max_iterations"):
                kwargs["max_iterations"] = task["max_iterations"]
            findings = await agent_fn(full_task, head_sha, **kwargs)
        except Exception as e:
            logger.error("[%s] agent error: %s", agent_type, e)
            agent_status = "failed"

        duration_ms = int(asyncio.get_event_loop().time() * 1000) - start_ms

        # Step Checkpoint 写入
        if task_id and is_single_batch:
            try:
                from src.db.repository import save_agent_result
                await save_agent_result(
                    task_id, agent_type, findings,
                    tokens_in=token_stats.get("tokens_in", 0),
                    tokens_out=token_stats.get("tokens_out", 0),
                    duration_ms=duration_ms,
                    status=agent_status,
                )
                logger.info(
                    "[%s] checkpoint saved: findings=%d duration_ms=%d status=%s",
                    agent_type, len(findings), duration_ms, agent_status,
                )
            except Exception as e:
                logger.warning("[%s] checkpoint save failed (non-critical): %s", agent_type, e)

        return findings

    # 并行执行
    try:
        results = await asyncio.gather(*[_run_one(t) for t in tasks])
    except Exception as e:
        logger.error("run_agents_node gather failed: %s", e)
        results = [[] for _ in tasks]

    return {"findings": [f for r in results for f in r]}
