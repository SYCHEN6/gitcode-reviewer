"""真实端到端测试 — 向本地 Webhook 发命令，等待后台任务完成，从 GitCode 评论区验证结果。

测试步骤：
  [1] /ai help      — 立即验证（等待 30s，检查帮助评论出现）
  [2] /ai explain   — 先发评论获取 note_id，再发 Webhook，等待 90s 检查 edit-in-place
  [3] /ai summary   — 等待 120s，检查 AI 摘要评论出现或更新
  [4] /ai review    — 等待 300s，检查 inline comments 出现（--review 开关控制，默认跳过）

用法：
    python scripts/live_test.py --project-id chensiyu47/MindIE-SD_1344 --mr-iid 1
    python scripts/live_test.py --project-id chensiyu47/MindIE-SD_1344 --mr-iid 1 --review
"""
import argparse
import asyncio
import json
import sys
import time

import httpx

from _common import *

WEBHOOK_URL = "http://localhost:8080/webhook"
PASS = "✅ PASS"
FAIL = "❌ FAIL"

results: list[tuple[str, str, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    status = PASS if ok else FAIL
    results.append((name, status, detail))
    mark = "✅" if ok else "❌"
    print(f"  {mark}  {name}" + (f"\n       {detail}" if detail else ""))


# ── 工具函数 ──────────────────────────────────────────────────────────────────

def _webhook_headers(secret: str) -> dict:
    return {"Content-Type": "application/json", "X-Gitcode-Token": secret}


def _note_payload(project_id: str, mr_iid: int, body: str, note_id: int = 0) -> bytes:
    return json.dumps({
        "event": "Note Hook",
        "object_kind": "note",
        "project": {"path_with_namespace": project_id},
        "object_attributes": {
            "noteable_type": "MergeRequest",
            "note": body,
            "id": note_id,
        },
        "merge_request": {"iid": mr_iid},
    }).encode()


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


async def _send_webhook(payload: bytes, secret: str) -> int:
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.post(WEBHOOK_URL, content=payload, headers=_webhook_headers(secret))
        return r.status_code


async def _wait_for_condition(
    check_fn,
    timeout_s: int,
    poll_s: int = 5,
    label: str = "",
) -> tuple[bool, str]:
    """轮询直到 check_fn() 返回 (True, detail) 或超时。"""
    deadline = time.time() + timeout_s
    elapsed = 0
    while time.time() < deadline:
        ok, detail = await check_fn()
        if ok:
            return True, detail
        remaining = int(deadline - time.time())
        print(f"    ⏳  等待中... 已等 {elapsed}s，剩余 {remaining}s  ({label})", end="\r")
        await asyncio.sleep(poll_s)
        elapsed += poll_s
    print()
    return False, "超时"


# ── 测试步骤 ──────────────────────────────────────────────────────────────────

async def test_help(gc, project_id: str, mr_iid: int, secret: str) -> None:
    print("\n[1] /ai help ─────────────────────────────────────")
    before = await gc.get_pr_comments(project_id, mr_iid)
    before_ids = {c.get("id") or c.get("note_id") for c in before}

    payload = _note_payload(project_id, mr_iid, "/ai help")
    status = await _send_webhook(payload, secret)
    if status not in (200, 202):
        record("/ai help webhook 接收", False, f"status={status}")
        return
    record("/ai help webhook 接收", True, f"status={status}")

    async def _check():
        comments = await gc.get_pr_comments(project_id, mr_iid)
        for c in comments:
            cid = c.get("id") or c.get("note_id")
            body = c.get("body", "")
            if cid not in before_ids and "可用命令" in body:
                return True, f"comment_id={cid}"
        return False, ""

    ok, detail = await _wait_for_condition(_check, timeout_s=30, label="/ai help")
    print()
    record("/ai help 评论出现在 MR", ok, detail)


async def test_explain(gc, project_id: str, mr_iid: int, secret: str) -> None:
    print("\n[2] /ai explain (edit-in-place) ──────────────────")
    from src.webhook.handlers import _EXPLAIN_MARKER

    # 先发一条包含代码片段的评论，获取 note_id
    snippet = "def divide(a, b):\n    return a / b  # 未处理除零"
    comment_body = f"/ai explain\n```python\n{snippet}\n```"
    try:
        result = await gc.post_mr_note(project_id, mr_iid, comment_body)
        note_id = result.get("comment_id", 0)
        if not note_id:
            record("/ai explain 发起测试评论", False, "comment_id=0")
            return
        record("/ai explain 发起测试评论", True, f"comment_id={note_id}")
    except Exception as e:
        record("/ai explain 发起测试评论", False, str(e))
        return

    # 发 Webhook 触发 explain 处理
    payload = _note_payload(project_id, mr_iid, comment_body, note_id=note_id)
    status = await _send_webhook(payload, secret)
    record("/ai explain webhook 接收", status in (200, 202), f"status={status}")

    # 等待评论被 edit-in-place 更新（含 marker）
    async def _check():
        try:
            c = await gc.get_pr_comment(project_id, note_id)
            body = c.get("body", "")
            if _EXPLAIN_MARKER in body and "代码解释" in body:
                preview = body[body.find("代码解释"):body.find("代码解释") + 30]
                return True, f"marker found, preview: {preview!r}"
        except Exception:
            pass
        return False, ""

    ok, detail = await _wait_for_condition(_check, timeout_s=90, label="/ai explain")
    print()
    record("/ai explain edit-in-place 成功", ok, detail)


async def test_summary(gc, project_id: str, mr_iid: int, secret: str) -> None:
    print("\n[3] /ai summary ──────────────────────────────────")
    from src.graph.review_graph import _AI_SECTION_START

    before = await gc.get_pr_comments(project_id, mr_iid)
    # 找已有 AI 摘要评论的更新时间（如果有）
    ai_summary_before = next(
        (c for c in before if _AI_SECTION_START in (c.get("body") or "")), None
    )
    before_updated = ai_summary_before.get("updated_at") if ai_summary_before else None

    payload = _note_payload(project_id, mr_iid, "/ai summary")
    status = await _send_webhook(payload, secret)
    record("/ai summary webhook 接收", status in (200, 202), f"status={status}")

    async def _check():
        comments = await gc.get_pr_comments(project_id, mr_iid)
        for c in comments:
            body = c.get("body") or ""
            if _AI_SECTION_START not in body:
                continue
            updated = c.get("updated_at")
            cid = c.get("id") or c.get("note_id")
            # 新评论 OR 已有评论被更新
            if ai_summary_before is None:
                return True, f"new summary comment_id={cid}"
            if updated != before_updated:
                return True, f"summary updated comment_id={cid}"
        return False, ""

    ok, detail = await _wait_for_condition(_check, timeout_s=120, label="/ai summary")
    print()
    record("/ai summary AI 摘要出现/更新", ok, detail)


async def test_review(gc, project_id: str, mr_iid: int, secret: str) -> None:
    print("\n[4] /ai review (full multi-agent) ───────────────")
    from src.graph.review_graph import _AI_INLINE_RE

    try:
        diff_data = await gc.get_pr_diff(project_id, mr_iid)
        commit_sha = diff_data.get("head_sha", "live-test")
    except Exception as e:
        record("/ai review 获取 commit_sha", False, str(e))
        return

    before = await gc.get_pr_comments(project_id, mr_iid)
    before_ids = {c.get("id") or c.get("note_id") for c in before}

    # 用 merge_request 事件触发（模拟 push/open）
    payload = _mr_payload(project_id, mr_iid, f"live-{int(time.time())}")
    status = await _send_webhook(payload, secret)
    record("/ai review webhook 接收", status in (200, 202), f"status={status}")

    async def _check():
        comments = await gc.get_pr_comments(project_id, mr_iid)
        new_inline = [
            c for c in comments
            if (c.get("id") or c.get("note_id")) not in before_ids
            and _AI_INLINE_RE.search(c.get("body") or "")
        ]
        if new_inline:
            return True, f"{len(new_inline)} 条新 inline comment"
        return False, ""

    ok, detail = await _wait_for_condition(_check, timeout_s=300, poll_s=10, label="/ai review")
    print()
    record("/ai review inline comment 出现", ok, detail)


# ── Main ──────────────────────────────────────────────────────────────────────

async def main(args: argparse.Namespace) -> None:
    from src.tools.gitcode_client import GitCodeClient
    from src.config import settings

    gc = GitCodeClient(settings.GITCODE_BASE_URL, settings.GITCODE_TOKEN)
    secret = settings.WEBHOOK_SECRET
    pid = args.project_id
    mr = args.mr_iid

    print(f"实测目标: {pid} MR#{mr}")
    print(f"Webhook:  {WEBHOOK_URL}")

    await test_help(gc, pid, mr, secret)
    await test_explain(gc, pid, mr, secret)
    await test_summary(gc, pid, mr, secret)
    if args.review:
        await test_review(gc, pid, mr, secret)
    else:
        print("\n[4] /ai review — 跳过（加 --review 开启）")

    print("\n─────────────────────────────────────────────────")
    passed = sum(1 for _, s, _ in results if s == PASS)
    failed = sum(1 for _, s, _ in results if s == FAIL)
    total = len(results)
    print(f"结果: {passed}/{total} 通过" + (f"，{failed} 失败" if failed else " 🎉"))

    if failed:
        print("\n失败项：")
        for name, status, detail in results:
            if status == FAIL:
                print(f"  ❌  {name}: {detail}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-id", default="chensiyu47/MindIE-SD_1344")
    parser.add_argument("--mr-iid", type=int, default=1)
    parser.add_argument("--review", action="store_true", help="也测试完整 /ai review（约 5 分钟）")
    asyncio.run(main(parser.parse_args()))
