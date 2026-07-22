"""全功能验证脚本 — 分 3 层测试。

Layer 1: 单元层（不调 API，纯逻辑）
  - token stats 写入 _token_stats dict
  - synthesize_node 保留 finding_id
  - risk recalculation 函数存在且可调用

Layer 2: API 层（直接调 GitCode REST，不走 webhook）
  - get_pr_diff
  - get_pr_comments（读现有评论）
  - post_mr_note + update_pr_comment（/ai explain edit-in-place 链路）

Layer 3: Webhook 层（通过 localhost:8080 触发，模拟 GitCode 投递）
  - /ai summary 命令
  - /ai help 命令

用法:
    python scripts/verify_all_features.py --project-id chensiyu47/MindIE-SD_1344 --mr-iid 1
    python scripts/verify_all_features.py --project-id chensiyu47/MindIE-SD_1344 --mr-iid 1 --layer 1
"""

import argparse
import asyncio
import hashlib
import hmac
import json
import logging
import sys
import time
import uuid
from unittest.mock import AsyncMock, patch

from _common import *

logging.basicConfig(level=logging.WARNING)

PASS = "✅ PASS"
FAIL = "❌ FAIL"
SKIP = "⏭  SKIP"
results: list[tuple[str, str, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    status = PASS if ok else FAIL
    results.append((name, status, detail))
    print(f"  {status}  {name}" + (f"  ({detail})" if detail else ""))


# ═══════════════════════════════════════════════════════════════
# Layer 1: 单元层
# ═══════════════════════════════════════════════════════════════

async def test_layer1() -> None:
    print("\n── Layer 1: 单元层 ──────────────────────────────")

    # 1a. token stats 写回 _token_stats
    from src.agents.expert_agent import _parse_findings, run_expert_agent

    async def _check_token_stats():
        token_stats: dict = {}
        dummy_task = {
            "project_id": "owner/repo",
            "files": ["foo.py"],
            "focus_hint": "",
            "diff_slice": "",
            "new_files": [],
            "languages": [],
            "_file_cache": {},
            "_token_stats": token_stats,
        }
        # Mock LLM to return [] immediately (no tool calls)
        mock_resp = AsyncMock()
        mock_resp.tool_calls = []
        mock_resp.content = "[]"
        mock_resp.usage_metadata = {"input_tokens": 42, "output_tokens": 17}

        with patch("src.agents.expert_agent._llm_invoke_with_retry", return_value=mock_resp), \
             patch("src.agents.expert_agent.GitCodeClient"):
            from src.agents.expert_agent import run_expert_agent
            result = await run_expert_agent("QualityAgent", "sys", dummy_task, "sha123")

        return token_stats

    stats = await _check_token_stats()
    ok = stats.get("tokens_in") == 42 and stats.get("tokens_out") == 17
    record("token_stats 写回 _token_stats dict", ok,
           f"got {stats}" if not ok else f"in={stats['tokens_in']} out={stats['tokens_out']}")

    # 1b. synthesize_node 保留 finding_id
    from src.graph.review_graph import synthesize_node
    fid = str(uuid.uuid4())
    state = {
        "findings": [
            {"agent": "QualityAgent", "file": "a.py", "line_start": 10,
             "severity": "HIGH", "description": "issue", "finding_id": fid},
        ],
        "diffs": [],
    }
    result = synthesize_node(state)
    ok = (
        len(result["final_findings"]) == 1
        and result["final_findings"][0].get("finding_id") == fid
    )
    record("synthesize_node 保留 finding_id", ok)

    # 1c. repository 新函数可导入
    try:
        from src.db.repository import get_open_mr_ids, count_open_critical_high
        record("repository.get_open_mr_ids / count_open_critical_high 可导入", True)
    except ImportError as e:
        record("repository.get_open_mr_ids / count_open_critical_high 可导入", False, str(e))

    # 1d. save_suggestion 接受 severity 参数
    import inspect
    from src.db.repository import save_suggestion
    sig = inspect.signature(save_suggestion)
    has_severity = "severity" in sig.parameters
    record("save_suggestion 包含 severity 参数", has_severity)

    # 1e. handlers._mark_suggestions_applied 包含 risk 重算逻辑
    import importlib, ast
    handlers_path = Path(__file__).parent.parent / "src" / "webhook" / "handlers.py"
    src = handlers_path.read_text(encoding="utf-8")
    has_recalc = "count_open_critical_high" in src and "ai-risk-" in src
    record("handlers._mark_suggestions_applied 包含 risk 重算逻辑", has_recalc)

    # 1f. _rule_engine_dispatch 定义在 review_graph
    from src.graph.review_graph import _rule_engine_dispatch
    tasks = _rule_engine_dispatch(["auth/login.py"], ["Python"], "small", {})
    has_security = any(t.get("agent_type") == "SecurityAgent" for t in tasks)
    record("_rule_engine_dispatch 位于 review_graph，auth 路径触发 SecurityAgent", has_security)


# ═══════════════════════════════════════════════════════════════
# Layer 2: API 层
# ═══════════════════════════════════════════════════════════════

async def test_layer2(project_id: str, mr_iid: int) -> None:
    print("\n── Layer 2: API 层 ──────────────────────────────")
    from src.tools.gitcode_client import GitCodeClient
    from src.config import settings

    gc = GitCodeClient(settings.GITCODE_BASE_URL, settings.GITCODE_TOKEN)

    # 2a. get_pr_diff
    try:
        diff_data = await gc.get_pr_diff(project_id, mr_iid)
        files = diff_data.get("files", [])
        head_sha = diff_data.get("head_sha", "")
        ok = bool(head_sha) and isinstance(files, list)
        record("get_pr_diff", ok, f"files={len(files)} head_sha={head_sha[:8]}")
    except Exception as e:
        record("get_pr_diff", False, str(e))
        return  # 后续依赖 diff，失败则跳过

    # 2b. get_pr_comments
    try:
        comments = await gc.get_pr_comments(project_id, mr_iid)
        record("get_pr_comments", True, f"{len(comments)} 条现有评论")
    except Exception as e:
        record("get_pr_comments", False, str(e))
        comments = []

    # 2c. post_mr_note（发一条测试评论）
    test_body = f"[verify_all_features] Layer 2 API test @ {time.strftime('%H:%M:%S')}"
    try:
        result = await gc.post_mr_note(project_id, mr_iid, test_body)
        note_id = result.get("comment_id", 0)
        ok = note_id != 0
        record("post_mr_note", ok, f"comment_id={note_id}")
    except Exception as e:
        record("post_mr_note", False, str(e))
        note_id = 0

    # 2d. get_pr_comment（单条读取）
    if note_id:
        try:
            c = await gc.get_pr_comment(project_id, note_id)
            ok = c.get("body", "") == test_body
            record("get_pr_comment", ok, f"body match={ok}")
        except Exception as e:
            record("get_pr_comment", False, str(e))

    # 2e. update_pr_comment（edit-in-place 核心路径）
    if note_id:
        marker = "<!-- __AI_EXPLAIN_APPENDED_7f3a__ -->"
        updated_body = f"{test_body}\n\n{marker}\n\n💡 测试解释内容"
        try:
            await gc.update_pr_comment(project_id, mr_iid, note_id, updated_body)
            # 回读验证
            c = await gc.get_pr_comment(project_id, note_id)
            ok = marker in c.get("body", "")
            record("update_pr_comment (edit-in-place)", ok)
        except Exception as e:
            record("update_pr_comment (edit-in-place)", False, str(e))


# ═══════════════════════════════════════════════════════════════
# Layer 3: Webhook 层
# ═══════════════════════════════════════════════════════════════

async def test_layer3(project_id: str, mr_iid: int) -> None:
    print("\n── Layer 3: Webhook 层（localhost:8080）────────────")
    import httpx
    from src.config import settings

    webhook_url = "http://localhost:8080/webhook"
    secret = settings.WEBHOOK_SECRET if hasattr(settings, "WEBHOOK_SECRET") else ""

    def _make_note_payload(body: str, note_id_val: int = 99999) -> dict:
        return {
            "event": "Note Hook",
            "object_kind": "note",
            "project": {"path_with_namespace": project_id},
            "object_attributes": {
                "noteable_type": "MergeRequest",
                "note": body,
                "id": note_id_val,
            },
            "merge_request": {"iid": mr_iid},
        }

    def _headers(payload_bytes: bytes) -> dict:
        return {
            "Content-Type": "application/json",
            "X-Gitcode-Token": secret or "test-token",
        }

    async def _post(payload: dict) -> tuple[int, dict]:
        body = json.dumps(payload).encode()
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(webhook_url, content=body, headers=_headers(body))
            try:
                return r.status_code, r.json()
            except Exception:
                return r.status_code, {}

    # 3a. /ai help
    payload = _make_note_payload("/ai help")
    status, resp = await _post(payload)
    ok = status in (200, 202)
    record("/ai help webhook 返回 2xx", ok, f"status={status}")

    # 3b. /ai summary
    payload = _make_note_payload("/ai summary")
    status, resp = await _post(payload)
    ok = status in (200, 202)
    record("/ai summary webhook 返回 2xx", ok, f"status={status} resp={resp}")

    # 3c. /ai explain（代码片段模式）— 使用一个不存在的 note_id，只测路由
    snippet = "def foo():\n    x = 1/0\n    return x"
    note_body = f"/ai explain\n```python\n{snippet}\n```"
    payload = _make_note_payload(note_body, note_id_val=88888)
    status, resp = await _post(payload)
    ok = status in (200, 202)
    record("/ai explain (snippet) webhook 返回 2xx", ok, f"status={status}")

    # 3d. 无效命令 — 应该静默忽略
    payload = _make_note_payload("普通评论，不是命令")
    status, resp = await _post(payload)
    ok = status in (200, 202)
    record("普通评论 webhook 静默忽略（2xx）", ok, f"status={status}")


# ═══════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════

async def _async_main(args: argparse.Namespace) -> None:
    layer = args.layer

    if layer in (0, 1):
        await test_layer1()
    if layer in (0, 2):
        await test_layer2(args.project_id, args.mr_iid)
    if layer in (0, 3):
        await test_layer3(args.project_id, args.mr_iid)

    print("\n─────────────────────────────────────────────────")
    passed = sum(1 for _, s, _ in results if s == PASS)
    failed = sum(1 for _, s, _ in results if s == FAIL)
    total = len(results)
    print(f"结果: {passed}/{total} 通过" + (f"，{failed} 失败" if failed else " 🎉"))

    if failed:
        print("\n失败项：")
        for name, status, detail in results:
            if status == FAIL:
                print(f"  {status}  {name}: {detail}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-id", default="chensiyu47/MindIE-SD_1344")
    parser.add_argument("--mr-iid", type=int, default=1)
    parser.add_argument("--layer", type=int, default=0,
                        help="0=全部, 1=单元, 2=API, 3=Webhook")
    args = parser.parse_args()

    print(f"验证目标: {args.project_id} MR#{args.mr_iid}  layer={args.layer or 'all'}")
    asyncio.run(_async_main(args))


if __name__ == "__main__":
    main()
