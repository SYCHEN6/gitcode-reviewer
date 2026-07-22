"""synthesize_node：去重合并 + critic_node：质量过滤。"""
import logging

from src.graph.diff_utils import (
    desc_should_merge,
    description_plausible,
    get_range_text,
    nearest_added_line,
    patch_text_for_file,
    severity_order,
)
from src.graph.state import ReviewState
from src.project_config import filter_findings_by_config

logger = logging.getLogger(__name__)


def _pick_better_finding(a: dict, b: dict) -> dict:
    """从两条"近行同主题" finding 中选择更好的一条保留。"""
    # 1. 优先保留 severity 更高的
    if severity_order(a.get("severity", "LOW")) < severity_order(b.get("severity", "LOW")):
        return a
    if severity_order(b.get("severity", "LOW")) < severity_order(a.get("severity", "LOW")):
        return b
    # 2. 同 severity：保留有 suggestion_code 的
    if a.get("suggestion_code") is not None and b.get("suggestion_code") is None:
        return a
    if b.get("suggestion_code") is not None and a.get("suggestion_code") is None:
        return b
    # 3. 保留描述更详细的
    return a if len(a.get("description", "")) >= len(b.get("description", "")) else b


def synthesize_node(state: ReviewState) -> dict:
    """对 findings 去重：
    - Step 1：同 Agent 同行只保留最高 severity
    - Step 1.5：不同 Agent 近行（±2 行）关键词重叠度 ≥ 0.25 → 合并
    - Step 2：跨 Agent 描述前 40 字相同 → 去重
    """
    # Step 1：同 Agent 同行保留最高 severity
    by_agent_line: dict[tuple, dict] = {}
    for f in state.get("findings", []):
        key = (f.get("agent", ""), f.get("file", ""), f.get("line_start", 0))
        prev = by_agent_line.get(key)
        if prev is None or severity_order(f.get("severity", "LOW")) < severity_order(prev.get("severity", "LOW")):
            by_agent_line[key] = f

    # Step 1.5：不同 Agent 近行关键词高度重叠 → 合并
    buckets: list[list[dict]] = []
    _LINE_TOLERANCE = 2

    for f in by_agent_line.values():
        file_f = f.get("file", "")
        line_f = f.get("line_start", 0)
        desc_f = f.get("description", "")
        absorbed = False
        for bucket in buckets:
            rep = bucket[0]
            if rep.get("file", "") != file_f:
                continue
            if abs(rep.get("line_start", 0) - line_f) > _LINE_TOLERANCE:
                continue
            if desc_should_merge(desc_f, rep.get("description", "")):
                bucket.append(f)
                absorbed = True
                break
        if not absorbed:
            buckets.append([f])

    merged: list[dict] = []
    for bucket in buckets:
        if len(bucket) == 1:
            merged.append(bucket[0])
        else:
            best = bucket[0]
            for f in bucket[1:]:
                best = _pick_better_finding(best, f)
            logger.debug(
                "synthesize: merged %d findings at %s:%s–%s → 1",
                len(bucket), best.get("file", ""), bucket[0].get("line_start", ""),
                bucket[-1].get("line_start", ""),
            )
            merged.append(best)

    # Step 2：跨 Agent 描述前 40 字去重
    seen: set[tuple] = set()
    unique: list[dict] = []
    for f in merged:
        dedup_key = (f.get("file", ""), f.get("line_start", 0), f.get("description", "")[:40])
        if dedup_key not in seen:
            seen.add(dedup_key)
            unique.append(f)

    final = sorted(unique, key=lambda f: severity_order(f.get("severity", "LOW")))
    return {"final_findings": final}


def critic_node(state: ReviewState) -> dict:
    """质量过滤：
    1. 去掉描述过短（< 10 字）或没有文件信息的 finding
    2. 去掉不对应本次 diff + 行的 finding
    3. 内容合理性检查：description 声称的关键词与实际代码行不符时丢弃
    """
    diffs = state.get("diffs", [])

    def _on_changed_line(f: dict) -> bool:
        fname = f.get("file", "")
        line_start = f.get("line_start", 0)
        if not fname or not line_start:
            return False
        patch_text = patch_text_for_file(diffs, fname)
        if not patch_text:
            return False
        return nearest_added_line(patch_text, line_start) is not None

    def _content_plausible(f: dict) -> bool:
        fname = f.get("file", "")
        line_start = f.get("line_start", 0)
        line_end = f.get("line_end", 0) or line_start
        description = f.get("description", "")
        if not fname or not line_start:
            return True
        patch_text = patch_text_for_file(diffs, fname)
        code_text = get_range_text(patch_text, line_start, line_end)
        return description_plausible(description, code_text)

    kept = []
    for finding in state.get("final_findings", []):
        if not finding.get("file"):
            continue
        if len(finding.get("description", "")) < 10:
            continue
        if not _on_changed_line(finding):
            logger.info(
                "critic_node: skip pre-existing finding %s:%s (not on diff + line)",
                finding.get("file", ""), finding.get("line_start", ""),
            )
            continue
        if not _content_plausible(finding):
            logger.info(
                "critic_node: skip implausible finding %s:%s (description/code mismatch)",
                finding.get("file", ""), finding.get("line_start", ""),
            )
            continue
        kept.append(finding)

    # Per-project min_severity + max_findings filter
    project_id = state.get("project_id", "")
    if project_id:
        kept = filter_findings_by_config(kept, project_id)

    return {"final_findings": kept}
