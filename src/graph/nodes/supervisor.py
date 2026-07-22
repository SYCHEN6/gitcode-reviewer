"""Supervisor 节点：首轮规则引擎派遣 + 后续 LLM 动态追查。"""
import logging

from src.agents.supervisor import get_focus_hints, run_supervisor
from src.graph.dispatch import enforce_tier_rules, rule_engine_dispatch
from src.graph.state import ReviewState
from src.project_config import get_enabled_agents, get_max_files

logger = logging.getLogger(__name__)


async def supervisor_node(state: ReviewState) -> dict:
    iteration = state.get("iteration", 0)
    tier = state.get("pr_stats", {}).get("tier", "medium")

    if iteration == 0:
        # 首轮：规则引擎派遣 + LLM Advisor 注入 focus_hints
        project_id = state["project_id"]
        files = state.get("file_list", [])
        languages = state.get("languages", [])
        pr_stats = state.get("pr_stats", {})

        # Per-project max_files：截断文件列表（0 = 不限制）
        max_files = get_max_files(project_id)
        if max_files > 0 and len(files) > max_files:
            logger.info("project config max_files=%d, truncating from %d files", max_files, len(files))
            files = files[:max_files]

        base_tasks = rule_engine_dispatch(files, languages, tier, pr_stats)

        # Per-project agent whitelist
        allowed = get_enabled_agents(project_id)
        if allowed:
            allowed_set = set(allowed)
            before = [t["agent_type"] for t in base_tasks]
            base_tasks = [t for t in base_tasks if t["agent_type"] in allowed_set]
            logger.info("project config agent whitelist=%s (was %s)", allowed, before)

        # small PR：跳过 LLM Advisor 节省 ~20s
        if tier == "small":
            hints: dict[str, str] = {}
        else:
            hints = await get_focus_hints(files, languages, pr_stats, base_tasks)
        for t in base_tasks:
            t["focus_hint"] = hints.get(t["agent_type"], "")

        tasks = enforce_tier_rules(base_tasks, tier)
        agent_names = sorted({t["agent_type"] for t in tasks})
        logger.info("supervisor[rule_engine] tier=%s agents=%s", tier, agent_names)
        return {
            "supervisor_action":    "DISPATCH",
            "agents_to_dispatch":   tasks,
            "supervisor_reasoning": [f"规则引擎首轮派遣（{tier}）: {agent_names}"],
            "pr_meta": state.get("pr_meta", {}),
        }

    # 后续轮：LLM 动态追查决策
    decision = await run_supervisor(state)
    tasks = enforce_tier_rules(decision.get("agents_to_dispatch", []), tier)
    logger.info(
        "supervisor[llm] tier=%s action=%s tasks=%d",
        tier, decision.get("action"), len(tasks),
    )
    return {
        "supervisor_action":    decision.get("action", "FINISH"),
        "agents_to_dispatch":   tasks,
        "supervisor_reasoning": [decision.get("reasoning", "")],
        "pr_meta": decision.get("pr_meta", state.get("pr_meta", {})),
    }
