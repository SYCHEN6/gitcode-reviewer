"""AI 检视报告格式化：Markdown 生成、评论解析、去重 key 提取。"""

import logging
import re
from datetime import datetime

logger = logging.getLogger(__name__)

# ── 常量 ─────────────────────────────────────────────────────────────────────

AI_SECTION_START = "<!-- AI-REVIEW-START -->"
AI_SECTION_END   = "<!-- AI-REVIEW-END -->"
SEV_EMOJI = {"CRITICAL": "🔴", "HIGH": "🟠", "MEDIUM": "🟡", "LOW": "🔵"}
AI_AGENTS = {"SecurityAgent", "LogicAgent", "QualityAgent", "PerformanceAgent"}

# 我们发出的 inline comment 格式："{emoji} **[{SEVERITY}]** `file:line`"
_AI_INLINE_RE = re.compile(r"[🔴🟠🟡🔵] \*\*\[(?:CRITICAL|HIGH|MEDIUM|LOW)\]\*\*")


# ── AI 总结评论 ─────────────────────────────────────────────────────────────

def find_ai_summary_comment(comments: list[dict]) -> dict | None:
    """从评论列表中找到 AI 总结评论（含 AI-REVIEW-START 标记的那一条）。"""
    for c in comments:
        if AI_SECTION_START in (c.get("body", "") or ""):
            return c
    return None


def parse_reported_keys(comments: list[dict]) -> set[tuple]:
    """从已有评论中提取已报告发现的位置 (file, line_start)，用于跨 review-run 去重。

    跨 run 去重粒度：(file, line) 二元组。
    同一行只要已有 AI inline comment，当前 run 就不再重复发布。
    """
    reported: set[tuple] = set()
    file_line_re = re.compile(r"`([^`:\n]+):(\d+)`")
    for c in comments:
        body = c.get("body", "") or ""
        if not _AI_INLINE_RE.search(body):
            continue
        m = file_line_re.search(body)
        if not m:
            continue
        reported.add((m.group(1), int(m.group(2))))
    return reported


def parse_run_count(body: str) -> int:
    """从 MR 描述中解析已有 AI 检视轮次，默认 0。"""
    m = re.search(r"第\s*(\d+)\s*次", body)
    return int(m.group(1)) if m else 0


def strip_ai_section(body: str) -> str:
    """移除描述中已有的 AI 检视段落（两个 HTML 注释标记之间）。"""
    start = body.find(AI_SECTION_START)
    if start == -1:
        return body
    end = body.find(AI_SECTION_END, start)
    after = body[end + len(AI_SECTION_END):] if end != -1 else ""
    return (body[:start] + after).strip()


def build_ai_section(
    summary: dict,
    all_findings: list[dict],
    new_findings: list[dict],
    skipped_findings: list[dict],
    run_count: int,
    now_str: str,
    pr_stats: dict | None = None,
) -> str:
    """生成 AI 检视 Markdown 段落（含整体评估 + 问题清单）。"""
    risk = summary.get("risk_level", "MEDIUM")
    impact = summary.get("impact_analysis", "")
    risk_reason = summary.get("risk_reason", "")
    focus_points = summary.get("focus_points", [])
    total_files = summary.get("total_files", 0)
    pr_stats = pr_stats or {}

    sev_counts: dict[str, int] = {"CRITICAL": 0, "HIGH": 0, "MEDIUM": 0, "LOW": 0}
    suggestion_count = 0
    for f in all_findings:
        sev_counts[f.get("severity", "LOW")] += 1
        if f.get("suggestion_code") is not None:
            suggestion_count += 1

    risk_emoji = SEV_EMOJI.get(risk, "⚪")
    total_issues = len(all_findings)
    skipped_count = len(skipped_findings)
    new_count = len(new_findings)

    dist_str = (
        f"🔴×{sev_counts['CRITICAL']} "
        f"🟠×{sev_counts['HIGH']} "
        f"🟡×{sev_counts['MEDIUM']} "
        f"🔵×{sev_counts['LOW']}"
    )
    skip_note = f"（本次新增 {new_count}，跳过重复 {skipped_count}）" if skipped_count > 0 else ""
    focus_text = "\n".join(f"- {p}" for p in focus_points) if focus_points else "- 无特殊关注点"

    # 问题清单
    skipped_keys = {(f.get("file", ""), f.get("line_start", 0)) for f in skipped_findings}
    issue_lines: list[str] = []
    for i, f in enumerate(all_findings, 1):
        sev = f.get("severity", "LOW")
        emoji = SEV_EMOJI.get(sev, "⚪")
        desc = f.get("description", "")
        if len(desc) > 60:
            desc = desc[:57] + "..."
        fname = f.get("file", "")
        line_s = f.get("line_start", 0)
        location = f"`{fname}:{line_s}`" if fname and line_s else ""
        if (fname, line_s) in skipped_keys:
            note = " （已有评论，跳过重复发布）"
        elif fname and line_s:
            note = " （详细评论已发布在对应代码行）"
        else:
            note = ""
        issue_lines.append(f"{i}. {emoji} **[{sev}]** {desc} — {location}{note}")

    issue_section = "\n".join(issue_lines) if issue_lines else "- 本次未发现问题"

    # xl PR 警告
    xl_warning = ""
    if pr_stats.get("tier") == "xl":
        lines_added   = pr_stats.get("lines_added", 0)
        lines_removed = pr_stats.get("lines_removed", 0)
        files_count   = pr_stats.get("files", 0)
        xl_warning = (
            f"\n> ⚠️ **PR 规模过大**：本次变更共 {files_count} 个文件、"
            f"{lines_added} 行新增 / {lines_removed} 行删除。"
            f"过大的 PR 会显著降低检视质量，建议拆分为多个独立的小 PR。\n"
        )

    parts = [
        AI_SECTION_START,
        f"## 🤖 AI 代码检视报告（第 {run_count} 次）",
        xl_warning,
        "### 📊 整体评估",
        "",
        "| 指标 | 详情 |",
        "|------|------|",
        f"| 风险等级 | {risk_emoji} **{risk}** |",
        f"| 变更文件 | {total_files} 个 |",
        f"| 发现问题 | **{total_issues} 个** {skip_note}|",
        f"| 严重程度分布 | {dist_str} |",
        f"| 代码建议 | {suggestion_count} 条 |",
        "",
        f"**影响分析：** {impact}",
        "",
        f"**风险原因：** {risk_reason}",
        "",
        "### 🔍 关注点",
        "",
        focus_text,
        "",
        f"### 📋 问题清单（共 {total_issues} 个）",
        "",
        issue_section,
        "",
        "---",
        f"*由 gitcode-reviewer 自动生成 · {now_str} · 第 {run_count} 次检视*",
        AI_SECTION_END,
    ]
    return "\n".join(parts)
