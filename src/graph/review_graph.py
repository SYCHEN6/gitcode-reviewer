"""LangGraph ReviewOrchestrator — Multi-Agent PR 自动检视主图。

图结构：
  supervisor_node
    ↓ DISPATCH → run_agents_node → supervisor_node（循环，最多 5 轮）
    ↓ FINISH   → summary_node → synthesize_node → critic_node → publish_node → END
"""

import asyncio
import logging
import operator
import re
from typing import Annotated, TypedDict

from langgraph.graph import END, StateGraph

from src.agents.performance_agent import run_performance_agent
from src.agents.quality_agent import run_quality_agent
from src.agents.logic_agent import run_logic_agent
from src.agents.security_agent import run_security_agent
from src.agents.summary_agent import run_summary_agent
from src.agents.supervisor import run_supervisor
from src.config import settings
from src.tools.gitcode_client import GitCodeClient

logger = logging.getLogger(__name__)

_AGENT_MAP = {
    "SecurityAgent":    run_security_agent,
    "LogicAgent":       run_logic_agent,
    "QualityAgent":     run_quality_agent,
    "PerformanceAgent": run_performance_agent,
}

_RISK_LABEL = {
    "CRITICAL": "ai-risk:high",
    "HIGH":     "ai-risk:high",
    "MEDIUM":   "ai-risk:low",
    "LOW":      "ai-risk:low",
}


# ── State ──────────────────────────────────────────────────────────────────

class ReviewState(TypedDict):
    # 输入
    project_id: str
    mr_iid:     int
    commit_sha: str

    # init 阶段填充
    raw_diff:  str
    file_list: list[str]
    diffs:     list[dict]
    head_sha:  str
    base_sha:  str

    # Supervisor 循环控制
    iteration:            int
    supervisor_action:    str
    supervisor_reasoning: Annotated[list[str], operator.add]
    pr_meta:              dict
    agents_to_dispatch:   list[dict]

    # 专家 Agent 聚合输出
    findings: Annotated[list[dict], operator.add]

    # 最终输出
    summary:        dict
    final_findings: list[dict]


# ── 工具函数 ───────────────────────────────────────────────────────────────

def _extract_diff_slice(diffs: list[dict], files: list[str]) -> str:
    """提取指定文件集合的 diff 片段。"""
    file_set = set(files)
    parts = []
    for d in diffs:
        fname = d.get("filename", "")
        if fname not in file_set:
            continue
        patch = d.get("patch", "")
        if isinstance(patch, dict):
            patch = patch.get("diff", "")
        if patch:
            parts.append(f"--- a/{d.get('previous_filename') or fname}\n+++ b/{fname}\n{patch}")
    return "\n".join(parts)


def _compute_diff_position(patch_text: str, target_line: int) -> int | None:
    """将文件行号转换为 diff 行偏移量（GitCode v5 inline comment position）。"""
    position = 0
    new_line = 0
    in_hunk = False

    for line in patch_text.splitlines():
        if line.startswith("@@"):
            in_hunk = True
            m = re.search(r"\+(\d+)", line)
            if m:
                new_line = int(m.group(1)) - 1
            position += 1
            continue
        if not in_hunk:
            continue
        position += 1
        if line.startswith("-"):
            continue
        new_line += 1
        if new_line == target_line:
            return position
    return None


def _patch_text_for_file(diffs: list[dict], filename: str) -> str:
    for d in diffs:
        if d.get("filename") == filename:
            patch = d.get("patch", "")
            if isinstance(patch, dict):
                patch = patch.get("diff", "")
            return patch or ""
    return ""


def _severity_order(s: str) -> int:
    return {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}.get(s, 4)


# ── 节点实现 ───────────────────────────────────────────────────────────────

async def supervisor_node(state: ReviewState) -> dict:
    decision = await run_supervisor(state)
    return {
        "supervisor_action":    decision.get("action", "FINISH"),
        "agents_to_dispatch":   decision.get("agents_to_dispatch", []),
        "supervisor_reasoning": [decision.get("reasoning", "")],
        "pr_meta": decision.get("pr_meta", state.get("pr_meta", {})),
    }


async def run_agents_node(state: ReviewState) -> dict:
    tasks = state.get("agents_to_dispatch", [])
    head_sha = state.get("head_sha", "")
    diffs = state.get("diffs", [])
    project_id = state["project_id"]
    mr_iid = state["mr_iid"]

    async def _run_one(task: dict) -> list[dict]:
        agent_type = task.get("agent_type", "")
        agent_fn = _AGENT_MAP.get(agent_type)
        if not agent_fn:
            logger.warning("Unknown agent_type: %s", agent_type)
            return []
        files = task.get("files", [])
        full_task = {
            **task,
            "project_id": project_id,
            "mr_iid":     mr_iid,
            "diff_slice": _extract_diff_slice(diffs, files),
        }
        try:
            return await agent_fn(full_task, head_sha)
        except Exception as e:
            logger.error("[%s] agent error: %s", agent_type, e)
            return []

    results = await asyncio.gather(*[_run_one(t) for t in tasks], return_exceptions=True)

    all_findings: list[dict] = []
    for r in results:
        if isinstance(r, list):
            all_findings.extend(r)
        elif isinstance(r, Exception):
            logger.error("Agent gather exception: %s", r)

    return {
        "findings":  all_findings,
        "iteration": state.get("iteration", 0) + 1,
    }


async def summary_node(state: ReviewState) -> dict:
    summary = await run_summary_agent(
        file_list=state.get("file_list", []),
        raw_diff=state.get("raw_diff", ""),
        findings=state.get("findings", []),
    )
    return {"summary": summary}


def synthesize_node(state: ReviewState) -> dict:
    """去重 + 按 severity 排序。去重键：(file, line_start, category)。"""
    seen: dict[tuple, dict] = {}
    for f in state.get("findings", []):
        key = (f.get("file", ""), f.get("line_start", 0), f.get("category", ""))
        existing = seen.get(key)
        if existing is None or _severity_order(f.get("severity", "LOW")) < _severity_order(existing.get("severity", "LOW")):
            seen[key] = f

    final = sorted(seen.values(), key=lambda f: _severity_order(f.get("severity", "LOW")))
    return {"final_findings": final}


def critic_node(state: ReviewState) -> dict:
    """质量过滤：去掉描述过短（< 10 字）或没有文件信息的 finding。"""
    kept = [
        f for f in state.get("final_findings", [])
        if f.get("file") and len(f.get("description", "")) >= 10
    ]
    return {"final_findings": kept}


async def publish_node(state: ReviewState) -> dict:
    """将 final_findings + summary 写回 GitCode。"""
    gc = GitCodeClient(settings.GITCODE_BASE_URL, settings.GITCODE_TOKEN)
    project_id = state["project_id"]
    mr_iid = state["mr_iid"]
    head_sha = state.get("head_sha", "")
    diffs = state.get("diffs", [])
    final_findings = state.get("final_findings", [])
    summary = state.get("summary", {})

    posted = 0
    for finding in final_findings:
        fname = finding.get("file", "")
        line_start = finding.get("line_start", 0)
        description = finding.get("description", "")
        suggestion = finding.get("suggestion_code", "")
        severity = finding.get("severity", "LOW")
        agent = finding.get("agent", "")

        # 构建 comment body
        sev_emoji = {"CRITICAL": "🔴", "HIGH": "🟠", "MEDIUM": "🟡", "LOW": "🔵"}.get(severity, "⚪")
        body = f"{sev_emoji} **[{severity}] [{agent}]** `{fname}:{line_start}`\n\n{description}"
        if suggestion:
            body += f"\n\n```suggestion\n{suggestion}\n```"

        # 尝试计算 diff position，失败则降级为全局评论
        patch_text = _patch_text_for_file(diffs, fname)
        diff_pos = _compute_diff_position(patch_text, line_start) if patch_text and line_start else None

        try:
            if diff_pos and head_sha and fname:
                await gc.post_inline_comment(
                    project_id, mr_iid, body,
                    {"head_sha": head_sha, "new_path": fname, "new_line": diff_pos},
                )
            else:
                await gc.post_mr_note(project_id, mr_iid, body)
            posted += 1
        except Exception as e:
            logger.error("publish comment failed (file=%s line=%s): %s", fname, line_start, e)
            # 降级重试
            try:
                await gc.post_mr_note(project_id, mr_iid, body)
                posted += 1
            except Exception as e2:
                logger.error("fallback note also failed: %s", e2)

    # 更新 MR 描述（追加摘要）
    if summary:
        risk = summary.get("risk_level", "MEDIUM")
        impact = summary.get("impact_analysis", "")
        risk_reason = summary.get("risk_reason", "")
        focus_points = summary.get("focus_points", [])
        fp_text = "\n".join(f"- {p}" for p in focus_points)
        sev_counts: dict[str, int] = {"CRITICAL": 0, "HIGH": 0, "MEDIUM": 0, "LOW": 0}
        for f in final_findings:
            sev_counts[f.get("severity", "LOW")] += 1
        desc = (
            f"\n\n---\n## 🤖 AI 代码检视摘要\n\n"
            f"**风险等级：** {risk}　|　"
            f"**问题数：** CRITICAL={sev_counts['CRITICAL']} HIGH={sev_counts['HIGH']} MEDIUM={sev_counts['MEDIUM']} LOW={sev_counts['LOW']}\n\n"
            f"**影响分析：** {impact}\n\n"
            f"**风险原因：** {risk_reason}\n\n"
            f"**关注点：**\n{fp_text}"
        )
        try:
            await gc.update_mr_description(project_id, mr_iid, desc)
        except Exception as e:
            logger.error("update_mr_description failed: %s", e)

        # 打风险标签（Phase 2：尝试打标签，无标签时跳过）
        label_name = _RISK_LABEL.get(risk)
        if label_name:
            try:
                existing = await gc.get_repo_labels(project_id)
                label_names = [lb["name"] for lb in existing]
                if label_name in label_names:
                    await gc.update_mr_label(project_id, mr_iid, [label_name])
                else:
                    logger.info("Label '%s' not found in repo, skipping", label_name)
            except Exception as e:
                logger.error("update_mr_label failed: %s", e)

    logger.info("publish_node done: posted=%d findings=%d", posted, len(final_findings))
    return {}


# ── 路由函数 ────────────────────────────────────────────────────────────────

def _route_supervisor(state: ReviewState) -> str:
    if state.get("supervisor_action") == "FINISH":
        return "summary"
    if state.get("iteration", 0) >= 5:
        logger.warning("Max iterations reached, forcing summary")
        return "summary"
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
        "summary":    "summary",
    })
    g.add_edge("run_agents", "supervisor")
    g.add_edge("summary",    "synthesize")
    g.add_edge("synthesize", "critic")
    g.add_edge("critic",     "publish")
    g.add_edge("publish",    END)
    return g


_graph = _build_graph().compile()


# ── 对外入口 ───────────────────────────────────────────────────────────────

async def run_review_graph(project_id: str, mr_iid: int, commit_sha: str) -> None:
    """Webhook handler 调用的入口，拉取 diff 后启动 LangGraph。"""
    gc = GitCodeClient(settings.GITCODE_BASE_URL, settings.GITCODE_TOKEN)

    try:
        diff_data = await gc.get_pr_diff(project_id, mr_iid)
    except Exception as e:
        logger.error("get_pr_diff failed for %s#%d: %s", project_id, mr_iid, e)
        return

    initial: dict = {
        "project_id": project_id,
        "mr_iid":     mr_iid,
        "commit_sha": commit_sha,
        "raw_diff":   diff_data.get("diff", ""),
        "file_list":  diff_data.get("files", []),
        "diffs":      diff_data.get("diffs", []),
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

    try:
        await _graph.ainvoke(initial)
        logger.info("review_graph completed for %s#%d", project_id, mr_iid)
    except Exception as e:
        logger.error("review_graph failed for %s#%d: %s", project_id, mr_iid, e)
