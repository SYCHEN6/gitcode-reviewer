"""Suggestion 功能端到端测试。

测试链路：
  [1] 触发 /ai review → 等待 inline comment（含 suggestion block）出现
  [2] 检查 suggestion_status 表是否写入新记录
  [3] 模拟 push 事件（commit message 含 "Apply suggestion"）
  [4] 验证 suggestion_status 记录变为 applied
  [5] 验证 MR 标签更新（ai-risk-high / ai-risk-low）

用法：
    python scripts/test_suggestion_flow.py
    python scripts/test_suggestion_flow.py --project-id chensiyu47/MindIE-SD_1344 --mr-iid 1
"""
import argparse
import asyncio
import json
import sys
import time

import httpx

from _common import *

WEBHOOK_URL = "http://localhost:8080/webhook"
PASS = "[PASS]"
FAIL = "[FAIL]"
results: list[tuple[str, str, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    status = PASS if ok else FAIL
    results.append((name, status, detail))
    mark = "OK " if ok else "NG "
    print(f"  {mark} {name}" + (f"  ({detail})" if detail else ""))


def _headers(secret: str) -> dict:
    return {"Content-Type": "application/json", "X-Gitcode-Token": secret}


def _mr_payload(project_id: str, mr_iid: int, commit_sha: str) -> bytes:
    return json.dumps({
        "event": "Merge Request Hook",
        "object_kind": "merge_request",
        "project": {"path_with_namespace": project_id},
        "object_attributes": {
            "iid": mr_iid,
            "action": "open",
            "last_commit": {"id": commit_sha},
        },
    }).encode()


def _push_payload(project_id: str, file_paths: list[str], commit_msg: str) -> bytes:
    return json.dumps({
        "event": "Push Hook",
        "object_kind": "push",
        "project": {"path_with_namespace": project_id},
        "commits": [{"message": commit_msg, "added": [], "modified": file_paths, "removed": []}],
    }).encode()


async def _send(payload: bytes, secret: str) -> int:
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.post(WEBHOOK_URL, content=payload, headers=_headers(secret))
        return r.status_code


async def _wait(check_fn, timeout_s: int, poll_s: int = 8, label: str = "") -> tuple[bool, str]:
    deadline = time.time() + timeout_s
    elapsed = 0
    while time.time() < deadline:
        ok, detail = await check_fn()
        if ok:
            return True, detail
        remaining = int(deadline - time.time())
        print(f"    ... wait {elapsed}s / {timeout_s}s  ({label})", end="\r")
        await asyncio.sleep(poll_s)
        elapsed += poll_s
    print()
    return False, "timeout"


# ─────────────────────────────────────────────────────────────────────────────
async def run_test(project_id: str, mr_iid: int) -> None:
    from src.tools.gitcode_client import GitCodeClient
    from src.config import settings
    from src.db import repository

    gc = GitCodeClient(settings.GITCODE_BASE_URL, settings.GITCODE_TOKEN)
    secret = settings.WEBHOOK_SECRET

    print(f"\nTarget: {project_id}  MR#{mr_iid}")
    print(f"Webhook: {WEBHOOK_URL}")

    # ── Step 1: 触发 review ────────────────────────────────────────────────
    print("\n[1] Trigger /ai review")
    commit_sha = f"sugg-test-{int(time.time())}"
    before_comments = await gc.get_pr_comments(project_id, mr_iid)
    before_ids = {c.get("id") or c.get("note_id") for c in before_comments}

    status = await _send(_mr_payload(project_id, mr_iid, commit_sha), secret)
    record("review webhook accepted", status in (200, 202), f"status={status}")
    if status not in (200, 202):
        return

    # ── Step 2: 等待 inline comment 出现 ──────────────────────────────────
    print("\n[2] Wait for inline comments (max 300s)")
    import re
    _AI_RE = re.compile(r"\[(?:CRITICAL|HIGH|MEDIUM|LOW)\]")

    new_inline: list[dict] = []

    async def _check_comments():
        nonlocal new_inline
        comments = await gc.get_pr_comments(project_id, mr_iid)
        new_inline = [
            c for c in comments
            if (c.get("id") or c.get("note_id")) not in before_ids
            and _AI_RE.search(c.get("body") or "")
        ]
        if new_inline:
            return True, f"{len(new_inline)} new inline comments"
        return False, ""

    ok, detail = await _wait(_check_comments, timeout_s=300, poll_s=10, label="inline comments")
    print()
    record("inline comments appeared", ok, detail)
    if not ok:
        print("  Review produced no inline comments — suggestion flow cannot be tested")
        return

    # 统计有 suggestion block 的评论
    sugg_comments = [c for c in new_inline if "```suggestion" in (c.get("body") or "")]
    print(f"  Inline comments with suggestion block: {len(sugg_comments)}/{len(new_inline)}")
    record("at least 1 suggestion block in comments", len(sugg_comments) > 0,
           f"{len(sugg_comments)} suggestion blocks found")

    # ── Step 3: 检查 suggestion_status 写入 ──────────────────────────────
    print("\n[3] Check suggestion_status DB records")
    await repository._ensure_tables()
    from sqlalchemy import text

    engine = repository._get_engine()
    async with engine.begin() as conn:
        r = await conn.execute(
            text("SELECT id, finding_id, file_path, line_start, severity, status, comment_id "
                 "FROM suggestion_status WHERE project_id=:pid AND mr_iid=:mr ORDER BY id DESC"),
            {"pid": project_id, "mr": mr_iid},
        )
        rows = r.fetchall()

    if rows:
        print(f"  suggestion_status: {len(rows)} records")
        for row in rows:
            print(f"    id={row[0]}  sev={row[4]}  status={row[5]}  file={row[2]}:{row[3]}"
                  f"  comment_id={row[6]}")
        record("suggestion_status records written", True, f"{len(rows)} records")
        pending_rows = [row for row in rows if row[5] == "pending"]
        record("status=pending", len(pending_rows) > 0, f"{len(pending_rows)} pending")
    else:
        print("  suggestion_status: empty")
        if len(sugg_comments) > 0:
            print("  WARNING: suggestion block comments posted but NOT saved to DB")
            print("  Possible causes: finding_id empty, task_id missing, or save_suggestion exception")
        record("suggestion_status records written", False, "0 records (check server log)")
        # Still continue to test apply flow if there are any pending rows from other tasks
        pending_rows = []

    # ── Step 4: 模拟 Apply suggestion push 事件 ──────────────────────────
    if not pending_rows:
        print("\n[4] No pending suggestions to apply — skipping apply test")
        return

    # 收集文件路径
    affected_files = list({row[2] for row in pending_rows if row[2]})
    print(f"\n[4] Simulate 'Apply suggestion' push for files: {affected_files}")
    push_msg = "Apply suggestion: fix issues flagged by AI review"
    status = await _send(_push_payload(project_id, affected_files, push_msg), secret)
    record("apply-suggestion push webhook accepted", status in (200, 202), f"status={status}")
    if status not in (200, 202):
        return

    # ── Step 5: 等待 suggestion_status 变为 applied ───────────────────────
    print("\n[5] Wait for suggestion_status → applied (max 30s)")
    finding_ids = {row[1] for row in pending_rows}

    async def _check_applied():
        async with engine.begin() as conn:
            r2 = await conn.execute(
                text("SELECT status FROM suggestion_status "
                     "WHERE project_id=:pid AND mr_iid=:mr AND status='applied'"),
                {"pid": project_id, "mr": mr_iid},
            )
            applied = r2.fetchall()
        if applied:
            return True, f"{len(applied)} applied"
        return False, ""

    ok, detail = await _wait(_check_applied, timeout_s=30, poll_s=3, label="applied")
    print()
    record("suggestion_status updated to applied", ok, detail)

    # ── Step 6: 验证 MR 标签 ──────────────────────────────────────────────
    print("\n[6] Check MR label updated")
    await asyncio.sleep(2)
    try:
        mr_info = await gc.get_pr_info(project_id, mr_iid)
        raw_labels = mr_info.get("labels") or []
        # labels 可能是 str 列表或 dict 列表（含 name 字段）
        label_names = [
            lb["name"] if isinstance(lb, dict) else str(lb)
            for lb in raw_labels
        ]
        label_str = ", ".join(label_names)
        print(f"  MR labels: {label_str or '(none)'}")
        has_risk_label = any("ai-risk" in lb for lb in label_names)
        record("MR has ai-risk label", has_risk_label, label_str)
    except Exception as e:
        record("get MR labels", False, str(e))


# ─────────────────────────────────────────────────────────────────────────────
async def main(args: argparse.Namespace) -> None:
    await run_test(args.project_id, args.mr_iid)

    print("\n─────────────────────────────────────────────────")
    passed = sum(1 for _, s, _ in results if s == PASS)
    failed = sum(1 for _, s, _ in results if s == FAIL)
    total = len(results)
    print(f"Result: {passed}/{total} passed" + (f", {failed} failed" if failed else " - all OK"))
    if failed:
        print("\nFailed:")
        for name, status, detail in results:
            if status == FAIL:
                print(f"  NG  {name}: {detail}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-id", default="chensiyu47/MindIE-SD_1344")
    parser.add_argument("--mr-iid", type=int, default=1)
    asyncio.run(main(parser.parse_args()))
