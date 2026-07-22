"""首轮规则引擎派遣 + tier 规则结构性纠正。

首轮使用规则引擎而非 LLM 的原因：
- LLM 决策在边界情况下输出格式不稳定
- 规则引擎保证 Agent 集合的确定性
- focus_hint 仍由 LLM 生成，保留语义理解能力
"""

import logging
import os
import re

from src.agents.logic_agent import run_logic_agent
from src.agents.performance_agent import run_performance_agent
from src.agents.quality_agent import run_quality_agent
from src.agents.security_agent import run_security_agent

logger = logging.getLogger(__name__)

# ── Agent → 函数映射 ────────────────────────────────────────────────────────

AGENT_MAP = {
    "SecurityAgent":    run_security_agent,
    "LogicAgent":       run_logic_agent,
    "QualityAgent":     run_quality_agent,
    "PerformanceAgent": run_performance_agent,
}

# ── 风险标签映射 ────────────────────────────────────────────────────────────

RISK_LABEL = {
    "CRITICAL": "ai-risk-high",
    "HIGH":     "ai-risk-high",
    "MEDIUM":   "ai-risk-low",
    "LOW":      "ai-risk-low",
}

LABEL_COLOR = {
    "ai-risk-high": "e11d48",  # 红色
    "ai-risk-low":  "f59e0b",  # 橙黄色
}

# ── 路径关键词 ─────────────────────────────────────────────────────────────

_ML_KEYWORDS = frozenset({
    "layer", "layers", "model", "models", "attn", "attention",
    "flash_attn", "train", "trainer", "training", "inference",
    "module", "backbone", "encoder", "decoder", "embedding",
})
_SECURITY_KEYWORDS = frozenset({
    "auth", "authentication", "authz", "authorization",
    "crypto", "encrypt", "decrypt", "hash", "hmac", "sign",
    "login", "logout", "session", "password", "passwd",
    "token", "jwt", "oauth", "apikey", "secret",
    "permission", "acl", "rbac", "policy",
})
_SECURITY_EXTENSIONS = frozenset({".sql"})

# ── 任务拆分常量 ────────────────────────────────────────────────────────────

_MAX_FILES_PER_TASK = 5   # large/xl PR 每个 Agent 任务的最大文件数
# 这些 Agent 在多文件任务中易忽略较小文件，强制按文件拆批保证覆盖率
_SPLIT_BY_FILE_AGENTS = {"QualityAgent", "PerformanceAgent"}


# ── 派遣 ────────────────────────────────────────────────────────────────────

def rule_engine_dispatch(
    files: list[str],
    languages: list[str],
    tier: str,
    pr_stats: dict,
) -> list[dict]:
    """首轮确定性 Agent 派遣（不依赖 LLM，0 延迟）。

    规则优先级（由高到低）：
    1. medium/large/xl → 全派
    2. 路径含 ML 关键词 → 加 PerformanceAgent
    3. 路径含安全关键词 / .sql 扩展名 → 加 SecurityAgent
    4. 其余 → 只派 QualityAgent + LogicAgent
    """
    needs_security = tier in ("medium", "large", "xl")
    needs_performance = tier in ("medium", "large", "xl")

    for fpath in files:
        lower = fpath.lower()
        _, ext = os.path.splitext(lower)
        tokens = set(re.split(r"[/_\-.]", lower))

        if tokens & _ML_KEYWORDS:
            needs_performance = True
        if tokens & _SECURITY_KEYWORDS or ext in _SECURITY_EXTENSIONS:
            needs_security = True

        if needs_security and needs_performance:
            break

    agents: list[str] = ["QualityAgent", "LogicAgent"]
    if needs_security:
        agents.insert(0, "SecurityAgent")
    if needs_performance:
        agents.append("PerformanceAgent")

    seen: set[str] = set()
    ordered = []
    for a in ["SecurityAgent", "LogicAgent", "QualityAgent", "PerformanceAgent"]:
        if a in agents and a not in seen:
            seen.add(a)
            ordered.append(a)

    return [{"agent_type": t, "files": files, "focus_hint": ""} for t in ordered]


def enforce_tier_rules(tasks: list[dict], tier: str) -> list[dict]:
    """对 Supervisor 调度决策做结构性纠正（不限制 Agent 类型，只做文件分批）。

    medium/large/xl：QualityAgent / PerformanceAgent 超过 1 个文件时按文件拆批。
    large / xl：所有 Agent 任务文件数超过上限时自动拆批。
    """
    split: list[dict] = []
    for task in tasks:
        files = task.get("files", [])
        agent_type = task.get("agent_type", "")

        if agent_type in _SPLIT_BY_FILE_AGENTS and len(files) > 1 and tier != "small":
            for f in files:
                split.append({**task, "files": [f]})
            continue

        if tier in ("large", "xl") and len(files) > _MAX_FILES_PER_TASK:
            for i in range(0, len(files), _MAX_FILES_PER_TASK):
                split.append({**task, "files": files[i:i + _MAX_FILES_PER_TASK]})
            continue

        split.append(task)

    return split
