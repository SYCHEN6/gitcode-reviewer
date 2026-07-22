"""publish_node：将 final_findings + summary 写回 GitCode。"""
import logging
from datetime import datetime

from src.config import settings
from src.graph.diff_utils import nearest_added_line, patch_text_for_file
from src.graph.dispatch import LABEL_COLOR, RISK_LABEL
from src.graph.formatting import (
    SEV_EMOJI,
    build_ai_section,
    find_ai_summary_comment,
    parse_reported_keys,
    parse_run_count,
)
from src.graph.state import ReviewState
from src.tools.gitcode_client import GitCodeClient

logger = logging.getLogger(__name__)


async def publish_node(state: ReviewState) -> dict:
    """将 final_findings + summary 写回 GitCode（跨轮去重 + 结构化描述）。"""
    gc = GitCodeClient(settings.GITCODE_BASE_URL, settings.GITCODE_TOKEN)
    project_id = state["project_id"]
    mr_iid = state["mr_iid"]
    head_sha = state.get("head_sha", "")
    diffs = state.get("diffs", [])
    final_findings = state.get("final_findings", [])
    summary = state.get("summary", {})
    task_id = state.get("task_id", "")

    # ── 1. 跨轮去重 ──────────────────────────────────────────────────────────
    existing_comments: list[dict] = []
    already_reported: set[tuple[str, int]] = set()
    try:
        existing_comments = await gc.get_pr_comments(project_id, mr_iid)
        already_reported = parse_reported_keys(existing_comments)
        logger.info("Found %d already-reported locations from existing comments", len(already_reported))
    except Exception as e:
        logger.warning("get_pr_comments failed, skipping dedup: %s", e)

    def _reported_key(f: dict) -> tuple:
        return (f.get("file", ""), f.get("line_start", 0))

    new_findings = [f for f in final_findings if _reported_key(f) not in already_reported]
    skipped_findings = [f for f in final_findings if _reported_key(f) in already_reported]
    logger.info(
        "publish_node: total=%d new=%d skipped=%d",
        len(final_findings), len(new_findings), len(skipped_findings),
    )

    # ── 2. 发布新发现评论 ────────────────────────────────────────────────────
    posted = 0
    for finding in new_findings:
        fname = finding.get("file", "")
        line_start = finding.get("line_start", 0)
        description = finding.get("description", "")
        suggestion = finding.get("suggestion_code", "")
        severity = finding.get("severity", "LOW")

        sev_emoji = SEV_EMOJI.get(severity, "⚪")
        body = f"{sev_emoji} **[{severity}]** `{fname}:{line_start}`\n\n{description}"
        if suggestion is not None:
            body += f"\n\n```suggestion\n{suggestion}\n```"

        patch_text = patch_text_for_file(diffs, fname)
        actual_line = nearest_added_line(patch_text, line_start) if patch_text and line_start else None

        try:
            if actual_line and head_sha and fname:
                result = await gc.post_inline_comment(
                    project_id, mr_iid, body,
                    {"head_sha": head_sha, "new_path": fname, "new_line": actual_line},
                )
            else:
                result = await gc.post_mr_note(project_id, mr_iid, body)
            posted += 1

            if finding.get("suggestion_code") is not None and task_id:
                finding_id = finding.get("finding_id", "")
                comment_id = result.get("comment_id", 0) if result else 0
                if finding_id:
                    try:
                        from src.db import repository as _repo
                        await _repo.save_suggestion(
                            task_id, finding_id, project_id, mr_iid,
                            comment_id, fname, line_start,
                            severity=severity,
                        )
                    except Exception as _db_exc:
                        logger.debug("save_suggestion skipped: %s", _db_exc)
        except Exception as e:
            logger.error("publish comment failed (file=%s line=%s): %s", fname, line_start, e)
            try:
                await gc.post_mr_note(project_id, mr_iid, body)
                posted += 1
            except Exception as e2:
                logger.error("fallback note also failed: %s", e2)

    # ── 3. AI 总结评论 ───────────────────────────────────────────────────────
    if summary:
        try:
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
            old_summary_comment = find_ai_summary_comment(existing_comments)
            run_count = (
                parse_run_count(old_summary_comment.get("body", "") or "")
                if old_summary_comment else 0
            ) + 1
            ai_section = build_ai_section(
                summary=summary,
                all_findings=final_findings,
                new_findings=new_findings,
                skipped_findings=skipped_findings,
                run_count=run_count,
                now_str=now_str,
                pr_stats=state.get("pr_stats"),
            )
            if old_summary_comment:
                await gc.update_pr_comment(
                    project_id, mr_iid,
                    old_summary_comment["id"],
                    ai_section,
                )
                logger.info("Updated existing AI summary comment (run #%d)", run_count)
            else:
                await gc.post_mr_note(project_id, mr_iid, ai_section)
                logger.info("Posted new AI summary comment (run #%d)", run_count)
        except Exception as e:
            logger.error("post/update AI summary comment failed: %s", e)

        # ── 4. 打风险标签 ────────────────────────────────────────────────────
        risk = summary.get("risk_level", "MEDIUM")
        label_name = RISK_LABEL.get(risk)
        if label_name:
            try:
                existing = await gc.get_repo_labels(project_id)
                label_names = {lb["name"] for lb in existing}
                label_ready = label_name in label_names
                if not label_ready:
                    color = LABEL_COLOR.get(label_name, "6b7280")
                    label_ready = await gc.create_label(project_id, label_name, color)
                    if label_ready:
                        logger.info("Label '%s' auto-created in repo", label_name)
                    else:
                        logger.warning(
                            "Label '%s' could not be created (API rejected), skipping MR label",
                            label_name,
                        )
                if label_ready:
                    await gc.update_mr_label(project_id, mr_iid, [label_name])
            except Exception as e:
                logger.error("update_mr_label failed: %s", e)

    logger.info("publish_node done: posted=%d skipped=%d", posted, len(skipped_findings))
    return {}
