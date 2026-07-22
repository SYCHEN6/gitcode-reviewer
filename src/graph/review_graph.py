"""LangGraph ReviewOrchestrator — Multi-Agent PR 自动检视主图。

图结构：
  supervisor_node
    ↓ DISPATCH → run_agents_node → supervisor_node（循环，最多 5 轮）
    ↓ FINISH   → summary_node → synthesize_node → critic_node → publish_node → END

模块拆分（P0 重构）：
  concurrency.py  — Redis 连接 / 分布式锁 / 信号量
  state.py        — ReviewState TypedDict
  dispatch.py     — 规则引擎派遣 / Agent 映射 / tier 规则
  diff_utils.py   — diff 解析 / 过滤 / 行号匹配 / 去重算法
  formatting.py   — AI 报告 Markdown 生成
  nodes/           — 各节点实现（supervisor / run_agents / synthesize / publish）
"""

import asyncio
import logging
from datetime import datetime

from langgraph.graph import END, StateGraph

from src.agents.summary_agent import run_summary_agent
from src.config import settings
from src.graph.concurrency import _distributed_global_semaphore, _distributed_mr_lock, write_review_metrics
from src.graph.diff_utils import calc_pr_stats, detect_languages, filter_reviewable_diffs
from src.graph.nodes import critic_node, publish_node, run_agents_node, supervisor_node, synthesize_node
from src.graph.state import ReviewState
from src.tools.gitcode_client import GitCodeClient

# ── 向后兼容：重新导出所有旧 API ────────────────────────────────────────────

# concurrency
from src.graph.concurrency import _get_redis  # noqa: E402, F401
from src.graph.concurrency import (
    _LOCK_TTL_SECONDS,           # noqa: F401
    _LUA_LOCK_RELEASE,            # noqa: F401
    _LUA_SEMAPHORE_ACQUIRE,       # noqa: F401
    _SEMAPHORE_REDIS_KEY,         # noqa: F401
    _SEMAPHORE_TTL,               # noqa: F401
    _distributed_global_semaphore,  # noqa: F401
    _distributed_mr_lock,         # noqa: F401
)

# diff_utils
from src.graph.diff_utils import (  # noqa: E402, F401
    SKIP_EXTENSIONS as _SKIP_EXTENSIONS,
    SKIP_BASENAMES as _SKIP_BASENAMES,
    SKIP_PATH_SEGMENTS as _SKIP_PATH_SEGMENTS,
    SKIP_NAME_SUFFIXES as _SKIP_NAME_SUFFIXES,
    DiffIndex,
    calc_pr_stats as _calc_pr_stats,
    compute_diff_position as _compute_diff_position,
    desc_keywords as _desc_keywords,
    desc_overlap as _desc_overlap,
    desc_should_merge as _desc_should_merge,
    description_plausible as _description_plausible,
    detect_languages as _detect_languages,
    detect_new_files as _detect_new_files,
    extract_diff_slice as _extract_diff_slice,
    filter_reviewable_diffs as _filter_reviewable_diffs,
    get_range_text as _get_range_text,
    is_reviewable as _is_reviewable,
    nearest_added_line as _nearest_added_line,
    patch_text_for_file as _patch_text_for_file,
    severity_order as _severity_order,
)

# dispatch
from src.graph.dispatch import (  # noqa: E402, F401
    AGENT_MAP as _AGENT_MAP,
    LABEL_COLOR as _LABEL_COLOR,
    RISK_LABEL as _RISK_LABEL,
    enforce_tier_rules as _enforce_tier_rules,
    rule_engine_dispatch as _rule_engine_dispatch,
)

# formatting
from src.graph.formatting import (  # noqa: E402, F401
    AI_AGENTS as _AI_AGENTS,
    AI_SECTION_END as _AI_SECTION_END,
    AI_SECTION_START as _AI_SECTION_START,
    SEV_EMOJI as _SEV_EMOJI,
    build_ai_section as _build_ai_section,
    find_ai_summary_comment as _find_ai_summary_comment,
    parse_reported_keys as _parse_reported_keys,
    parse_run_count as _parse_run_count,
    strip_ai_section as _strip_ai_section,
)
from src.graph.formatting import _AI_INLINE_RE  # noqa: F401

# nodes
from src.graph.nodes.synthesize import _pick_better_finding  # noqa: F401

logger = logging.getLogger(__name__)


# ── summary_node（轻量，不需单独文件）────────────────────────────────────────

async def summary_node(state: ReviewState) -> dict:
    files = state.get("file_list", [])
    raw_diff = state.get("raw_diff", "")
    findings = state.get("findings", [])
    summary = await run_summary_agent(files, raw_diff, findings)
    return {"summary": summary or {}}


# ── 路由函数 ────────────────────────────────────────────────────────────────

def _route_supervisor(state: ReviewState) -> str:
    if state.get("supervisor_action") == "FINISH":
        return "synthesize"
    if state.get("iteration", 0) >= 5:
        logger.warning("Max iterations reached, forcing synthesize")
        return "synthesize"
    return "run_agents"


# ── 图构建 ─────────────────────────────────────────────────────────────────

def _build_graph() -> StateGraph:
    g = StateGraph(ReviewState)
    g.add_node("supervisor", supervisor_node)
    g.add_node("run_agents", run_agents_node)
    g.add_node("summary", summary_node)
    g.add_node("synthesize", synthesize_node)
    g.add_node("critic", critic_node)
    g.add_node("publish", publish_node)

    g.set_entry_point("supervisor")
    g.add_conditional_edges("supervisor", _route_supervisor, {
        "run_agents": "run_agents",
        "synthesize": "synthesize",
    })
    g.add_edge("run_agents", "supervisor")
    g.add_edge("synthesize", "critic")
    g.add_edge("critic",     "summary")
    g.add_edge("summary",    "publish")
    g.add_edge("publish",    END)
    return g


_graph = _build_graph().compile()


# ── 对外入口 ───────────────────────────────────────────────────────────────

async def run_summary_only(project_id: str, mr_iid: int) -> None:
    """/ai summary 命令：仅生成 PR 摘要，不运行专家 Agent。"""
    from src.graph.formatting import (
        build_ai_section,
        find_ai_summary_comment,
        parse_run_count,
    )

    gc = GitCodeClient(settings.GITCODE_BASE_URL, settings.GITCODE_TOKEN)
    try:
        diff_data = await gc.get_pr_diff(project_id, mr_iid)
        raw_diff  = diff_data.get("diff", "")
        files     = diff_data.get("files", [])
        pr_stats  = calc_pr_stats(diff_data.get("diffs", []))
    except Exception as e:
        logger.error("run_summary_only: get_pr_diff failed: %s", e)
        return

    try:
        summary = await run_summary_agent(files, raw_diff, [])
    except Exception as e:
        logger.error("run_summary_only: summary_agent failed: %s", e)
        return

    try:
        existing = await gc.get_pr_comments(project_id, mr_iid)
        old_c    = find_ai_summary_comment(existing)
        run_count = (parse_run_count(old_c.get("body", "") or "") if old_c else 0) + 1
        now_str   = datetime.now().strftime("%Y-%m-%d %H:%M")
        ai_section = build_ai_section(
            summary=summary,
            all_findings=[],
            new_findings=[],
            skipped_findings=[],
            run_count=run_count,
            now_str=now_str,
            pr_stats=pr_stats,
        )
        if old_c:
            await gc.update_pr_comment(project_id, mr_iid, old_c["id"], ai_section)
            logger.info("run_summary_only: updated summary comment (run #%d)", run_count)
        else:
            await gc.post_mr_note(project_id, mr_iid, ai_section)
            logger.info("run_summary_only: posted summary comment (run #%d)", run_count)
    except Exception as e:
        logger.error("run_summary_only: post comment failed: %s", e)


async def run_review_graph(project_id: str, mr_iid: int, commit_sha: str) -> None:
    """Webhook handler 调用的入口，拉取 diff 后启动 LangGraph。

    并发控制策略（双层）：
    1. per-MR 锁：同一 MR 的多次 push 顺序执行
    2. 全局信号量：限制系统同时运行的检视任务总数
    """
    gc = GitCodeClient(settings.GITCODE_BASE_URL, settings.GITCODE_TOKEN)

    try:
        diff_data = await gc.get_pr_diff(project_id, mr_iid)
    except Exception as e:
        logger.error("get_pr_diff failed for %s#%d: %s", project_id, mr_iid, e)
        return

    diffs_all = diff_data.get("diffs", [])
    diffs_reviewable, diffs_skipped = filter_reviewable_diffs(diffs_all)
    if diffs_skipped:
        logger.info(
            "skip %d non-reviewable files: %s",
            len(diffs_skipped),
            [(s["file"], s["reason"]) for s in diffs_skipped],
        )

    pr_stats = calc_pr_stats(diffs_reviewable)
    languages = detect_languages(diffs_reviewable)
    logger.info(
        "PR stats: tier=%s files=%d lines_added=%d lines_removed=%d languages=%s reviewable=%s",
        pr_stats["tier"], pr_stats["files"], pr_stats["lines_added"], pr_stats["lines_removed"],
        languages, [d.get("filename") for d in diffs_reviewable],
    )

    # ── Step Checkpoint ──────────────────────────────────────────────────────
    task_id = ""
    if settings.MYSQL_URL:
        try:
            from src.db.repository import complete_task, create_or_get_task, fail_task
            task_id = await create_or_get_task(
                project_id, mr_iid, commit_sha,
                tier=pr_stats["tier"],
                languages=languages,
                total_files=len(diffs_reviewable),
            )
            logger.info("DB task created/resumed: task_id=%s", task_id)
        except Exception as e:
            logger.warning("DB unavailable, running without checkpoint: %s", e)

    initial: dict = {
        "project_id": project_id,
        "mr_iid":     mr_iid,
        "commit_sha": commit_sha,
        "task_id":    task_id,
        "raw_diff":   diff_data.get("diff", ""),
        "file_list":  [d.get("filename", "") for d in diffs_reviewable],
        "diffs":      diffs_reviewable,
        "pr_stats":   pr_stats,
        "languages":  languages,
        "head_sha":   diff_data.get("head_sha", ""),
        "base_sha":   diff_data.get("base_sha", ""),
        "iteration":            0,
        "supervisor_action":    "",
        "supervisor_reasoning": [],
        "pr_meta":              {},
        "agents_to_dispatch":   [],
        "findings":             [],
        "final_findings":       [],
        "summary":              {},
    }

    async with _distributed_mr_lock(project_id, mr_iid):
        async with _distributed_global_semaphore(settings.MAX_CONCURRENT_REVIEWS):
            logger.info(
                "review_graph start: project=%s mr=%d commit=%s task_id=%s languages=%s",
                project_id, mr_iid, commit_sha[:8], task_id or "none", languages,
            )
            start_time = asyncio.get_event_loop().time()
            try:
                final_state = await _graph.ainvoke(initial)
                total_ms = int((asyncio.get_event_loop().time() - start_time) * 1000)
                logger.info(
                    "review_graph completed: project=%s mr=%d total_ms=%d",
                    project_id, mr_iid, total_ms,
                )
                if task_id:
                    from src.db.repository import complete_task
                    await complete_task(task_id)
                await write_review_metrics(
                    project_id, mr_iid, commit_sha,
                    pr_stats, languages, final_state, total_ms,
                )
            except Exception as e:
                logger.error("review_graph failed for %s#%d: %s", project_id, mr_iid, e)
                if task_id:
                    try:
                        from src.db.repository import fail_task
                        await fail_task(task_id, str(e))
                    except Exception as fe:
                        logger.warning("fail_task also failed: %s", fe)
