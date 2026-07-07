"""验证去重 + 摘要发评论（不覆盖 MR 描述）两个功能。

验证点：
  [1] _parse_reported_keys：从已有评论中正确提取 AI 报告的 (file, line) 对
  [2] _find_ai_summary_comment：找到含 AI-REVIEW-START 标记的摘要评论
  [3] _build_ai_section：新摘要格式包含所有必要元素（彩灯 / 表格 / 问题清单）
  [4] publish_node 端到端（mock API）：首次发新摘要评论；复检时更新同一条评论
  [5] 真实 PR 全流程验证（触发 run_review_graph，观察评论区变化）

用法：
    python scripts/verify_dedup.py --project-id chensiyu47/MindIE-SD_1344 --mr-iid 1
    python scripts/verify_dedup.py --project-id chensiyu47/MindIE-SD_1344 --mr-iid 1 --real
"""

import argparse
import asyncio
import io
import logging
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger("src.graph").setLevel(logging.INFO)

logger = logging.getLogger("verify_dedup")

PASS = "[PASS]"
FAIL = "[FAIL]"


def section(title: str) -> None:
    print(f"\n{'─'*60}")
    print(f"  {title}")
    print(f"{'─'*60}")


# ─────────────────────────────────────────────────────────────
# [1] _parse_reported_keys
# ─────────────────────────────────────────────────────────────

def check_parse_reported_keys() -> bool:
    section("[1] _parse_reported_keys：提取已有 AI 评论的 (file, line) 对")
    from src.graph.review_graph import _parse_reported_keys

    # 模拟已有评论：2 条 AI 评论 + 1 条普通用户评论
    fake_comments = [
        {"body": "🟠 **[HIGH] [SecurityAgent]** `src/auth/login.py:45`\n\nSQL 注入风险"},
        {"body": "🔵 **[LOW] [QualityAgent]** `src/utils/helpers.py:12`\n\n命名不规范"},
        {"body": "LGTM，已理解"},          # 普通用户评论，不含 Agent 标记
        {"body": ""},                      # 空评论
    ]

    keys = _parse_reported_keys(fake_comments)
    print(f"  提取到已报告位置: {keys}")

    expected = {("src/auth/login.py", 45), ("src/utils/helpers.py", 12)}
    if keys == expected:
        print(f"  {PASS} 正确提取 2 个 AI 评论位置，普通用户评论被忽略")
        return True
    else:
        print(f"  {FAIL} 期望 {expected}，实际 {keys}")
        return False


# ─────────────────────────────────────────────────────────────
# [2] _find_ai_summary_comment
# ─────────────────────────────────────────────────────────────

def check_find_ai_summary_comment() -> bool:
    section("[2] _find_ai_summary_comment：从评论列表找 AI 摘要评论")
    from src.graph.review_graph import _AI_SECTION_START, _find_ai_summary_comment

    ai_comment = {"id": 200, "body": f"{_AI_SECTION_START}\n## 🤖 AI 代码检视报告（第 1 次）\n<!-- AI-REVIEW-END -->"}
    user_comment = {"id": 101, "body": "🟠 **[HIGH] [SecurityAgent]** `src/auth/login.py:45`\n\nSQL 注入风险"}
    empty_comment = {"id": 50, "body": ""}

    ok = True

    # 有 AI 摘要评论 → 应找到
    found = _find_ai_summary_comment([user_comment, ai_comment, empty_comment])
    if found and found["id"] == 200:
        print(f"  {PASS} 正确找到 AI 摘要评论（id=200）")
    else:
        print(f"  {FAIL} 未找到 AI 摘要评论，返回 {found}")
        ok = False

    # 无 AI 摘要评论 → 应返回 None
    not_found = _find_ai_summary_comment([user_comment, empty_comment])
    if not_found is None:
        print(f"  {PASS} 无摘要评论时正确返回 None")
    else:
        print(f"  {FAIL} 期望 None，实际 {not_found}")
        ok = False

    # 空列表 → 应返回 None
    if _find_ai_summary_comment([]) is None:
        print(f"  {PASS} 空列表返回 None")
    else:
        print(f"  {FAIL} 空列表应返回 None")
        ok = False

    return ok


# ─────────────────────────────────────────────────────────────
# [3] _build_ai_section 格式检查
# ─────────────────────────────────────────────────────────────

def check_build_ai_section() -> bool:
    section("[3] _build_ai_section：新摘要格式内容检查")
    from src.graph.review_graph import _AI_SECTION_END, _AI_SECTION_START, _build_ai_section

    summary = {
        "total_files": 3,
        "total_lines": 120,
        "impact_analysis": "修改了认证模块和数据库查询层",
        "risk_level": "HIGH",
        "risk_reason": "存在 SQL 注入和资源泄露",
        "focus_points": ["auth/login.py:45 SQL 注入", "db/connection.py:23 连接泄露"],
    }
    all_findings = [
        {"severity": "HIGH",   "file": "src/auth/login.py",   "line_start": 45,
         "description": "SQL 注入：用户输入直接拼接到查询", "agent": "SecurityAgent",
         "suggestion_code": "query = cursor.execute('SELECT...', (user_id,))"},
        {"severity": "MEDIUM", "file": "src/db/connection.py", "line_start": 23,
         "description": "数据库连接未在异常路径关闭",       "agent": "LogicAgent",
         "suggestion_code": ""},
        {"severity": "LOW",    "file": "src/utils/helpers.py", "line_start": 12,
         "description": "变量命名不规范：tmp 无意义",         "agent": "QualityAgent",
         "suggestion_code": ""},
    ]
    # 第 3 个 finding 已有评论（模拟跳过）
    new_findings = all_findings[:2]
    skipped_findings = all_findings[2:]

    result = _build_ai_section(
        summary=summary,
        all_findings=all_findings,
        new_findings=new_findings,
        skipped_findings=skipped_findings,
        run_count=2,
        now_str="2026-06-29 19:45",
    )

    print(f"\n{'─'*40} 生成的摘要段落 {'─'*40}")
    print(result)
    print(f"{'─'*80}\n")

    checks = [
        (_AI_SECTION_START in result,     "包含 AI-REVIEW-START 标记"),
        (_AI_SECTION_END in result,       "包含 AI-REVIEW-END 标记"),
        ("第 2 次" in result,             "标注第 2 次检视"),
        ("🟠" in result,                  "包含 HIGH 彩灯 🟠"),
        ("HIGH" in result,                "包含风险等级 HIGH"),
        ("3 个" in result,                "问题数为 3 个"),
        ("本次新增 2，跳过重复 1" in result, "标注新增/跳过数量"),
        ("3 条" in result,                "代码建议 3 条（含空字符串删除建议）"),
        ("详细评论已发布在对应代码行" in result, "新问题标注已发布"),
        ("已有评论，跳过重复发布" in result,     "跳过问题有明确标注"),
        ("`src/auth/login.py:45`" in result,   "问题清单含文件位置"),
        ("2026-06-29 19:45" in result,          "包含时间戳"),
    ]

    ok = True
    for cond, label in checks:
        mark = PASS if cond else FAIL
        print(f"  {mark} {label}")
        if not cond:
            ok = False

    return ok


# ─────────────────────────────────────────────────────────────
# [4] publish_node 端到端（全 Mock）
# ─────────────────────────────────────────────────────────────

async def check_publish_dedup() -> bool:
    section("[4] publish_node 端到端（mock API）")
    import src.graph.review_graph as rg_module
    from src.graph.review_graph import _AI_SECTION_END, _AI_SECTION_START, publish_node

    # ── Case A: 首次运行，无旧 AI 摘要评论 ──────────────────────────────────
    print("  Case A: 首次运行（无旧摘要评论）")
    existing_comments_a = [
        {"id": 101, "body": "🟠 **[HIGH] [SecurityAgent]** `src/auth/login.py:45`\n\nSQL 注入风险"},
    ]
    final_findings = [
        {"severity": "HIGH",   "file": "src/auth/login.py",   "line_start": 45,
         "description": "SQL 注入：用户输入直接拼接到查询", "agent": "SecurityAgent",
         "category": "security", "suggestion_code": "", "finding_id": "aaa"},
        {"severity": "MEDIUM", "file": "src/db/connection.py", "line_start": 23,
         "description": "数据库连接未在异常路径关闭",       "agent": "LogicAgent",
         "category": "logic",    "suggestion_code": "", "finding_id": "bbb"},
    ]
    state = {
        "project_id": "owner/repo",
        "mr_iid": 1,
        "head_sha": "abc123",
        "diffs": [],
        "final_findings": final_findings,
        "summary": {
            "total_files": 2,
            "risk_level": "HIGH",
            "impact_analysis": "测试影响分析",
            "risk_reason": "存在安全问题",
            "focus_points": ["auth/login.py SQL 注入"],
        },
    }

    posted_bodies_a: list[str] = []
    updated_comment_bodies_a: list[str] = []
    updated_desc_a: list[str] = []

    mock_gc_a = MagicMock()
    mock_gc_a.get_pr_comments = AsyncMock(return_value=existing_comments_a)
    mock_gc_a.post_inline_comment = AsyncMock(return_value={"comment_id": 1})
    mock_gc_a.post_mr_note = AsyncMock(side_effect=lambda pid, mriid, body: posted_bodies_a.append(body) or {"comment_id": 2})
    mock_gc_a.update_pr_comment = AsyncMock(side_effect=lambda pid, mriid, cid, body: updated_comment_bodies_a.append(body) or {"comment_id": cid})
    mock_gc_a.update_mr_description = AsyncMock(side_effect=lambda pid, mriid, body: updated_desc_a.append(body))
    mock_gc_a.get_repo_labels = AsyncMock(return_value=[])

    with patch.object(rg_module, "GitCodeClient", return_value=mock_gc_a):
        await publish_node(state)

    ok = True

    # 期望 post_mr_note 被调用 2 次：1 次 inline 降级 (connection.py:23) + 1 次 AI 摘要
    finding_notes_a = [b for b in posted_bodies_a if "connection.py:23" in b]
    summary_notes_a  = [b for b in posted_bodies_a if _AI_SECTION_START in b]

    if finding_notes_a:
        print(f"  {PASS} A: finding 评论发布（connection.py:23）")
    else:
        print(f"  {FAIL} A: 缺少 connection.py:23 finding 评论")
        ok = False

    if summary_notes_a and "第 1 次" in summary_notes_a[0]:
        print(f"  {PASS} A: AI 摘要作为新评论发布（第 1 次）")
    elif summary_notes_a:
        print(f"  {FAIL} A: 摘要已发但未标注「第 1 次」")
        ok = False
    else:
        print(f"  {FAIL} A: AI 摘要未发布到评论区")
        ok = False

    if updated_desc_a:
        print(f"  {FAIL} A: update_mr_description 被意外调用（不应修改 MR 描述）")
        ok = False
    else:
        print(f"  {PASS} A: MR 描述未被修改")

    if not updated_comment_bodies_a:
        print(f"  {PASS} A: update_pr_comment 未调用（首次无旧摘要，符合预期）")
    else:
        print(f"  {FAIL} A: update_pr_comment 被意外调用")
        ok = False

    # ── Case B: 复检，已存在 AI 摘要评论 ─────────────────────────────────────
    print(f"\n  Case B: 复检（已有 AI 摘要评论 id=200，应更新而非新建）")
    existing_comments_b = [
        {"id": 101, "body": "🟠 **[HIGH] [SecurityAgent]** `src/auth/login.py:45`\n\nSQL 注入风险"},
        {"id": 200, "body": f"{_AI_SECTION_START}\n## 🤖 AI 代码检视报告（第 1 次）\n{_AI_SECTION_END}"},
    ]

    posted_bodies_b: list[str] = []
    updated_comment_bodies_b: list[tuple] = []  # (comment_id, body)

    mock_gc_b = MagicMock()
    mock_gc_b.get_pr_comments = AsyncMock(return_value=existing_comments_b)
    mock_gc_b.post_inline_comment = AsyncMock(return_value={"comment_id": 1})
    mock_gc_b.post_mr_note = AsyncMock(side_effect=lambda pid, mriid, body: posted_bodies_b.append(body) or {"comment_id": 99})
    mock_gc_b.update_pr_comment = AsyncMock(side_effect=lambda pid, mriid, cid, body: updated_comment_bodies_b.append((cid, body)) or {"comment_id": cid})
    mock_gc_b.update_mr_description = AsyncMock()
    mock_gc_b.get_repo_labels = AsyncMock(return_value=[])

    with patch.object(rg_module, "GitCodeClient", return_value=mock_gc_b):
        await publish_node(state)

    summary_posted_b = [b for b in posted_bodies_b if _AI_SECTION_START in b]
    if not summary_posted_b:
        print(f"  {PASS} B: 未新建摘要评论（正确：应更新已有评论）")
    else:
        print(f"  {FAIL} B: 意外新建了摘要评论（应更新 id=200）")
        ok = False

    update_calls = [(cid, b) for cid, b in updated_comment_bodies_b if _AI_SECTION_START in b]
    if update_calls and update_calls[0][0] == 200:
        _, upd_body = update_calls[0]
        run_ok = "第 2 次" in upd_body
        skip_ok = "跳过重复 1" in upd_body
        print(f"  {PASS if run_ok else FAIL} B: 更新了 id=200 的摘要评论（第 2 次: {run_ok}）")
        print(f"  {PASS if skip_ok else FAIL} B: 摘要标注跳过重复 1 个: {skip_ok}")
        if not run_ok or not skip_ok:
            ok = False
    else:
        print(f"  {FAIL} B: update_pr_comment 未正确调用（id=200），实际: {update_calls}")
        ok = False

    return ok


# ─────────────────────────────────────────────────────────────
# [5] 真实 PR 全流程验证
# ─────────────────────────────────────────────────────────────

async def check_real_pr(project_id: str, mr_iid: int) -> bool:
    section(f"[5] 真实 PR 全流程验证 — {project_id}#{mr_iid}")
    import time

    from src.graph.review_graph import run_review_graph

    print(f"  触发 run_review_graph({project_id!r}, {mr_iid}, 'manual-verify')...")
    print("  （预计 30-60 秒）")
    t0 = time.time()
    try:
        await run_review_graph(project_id, mr_iid, "manual-verify")
        elapsed = time.time() - t0
        print(f"  {PASS} 完成，耗时 {elapsed:.1f}s")
        print("  请到 GitCode PR 页面确认：")
        print("    - MR 原始描述完整保留，未被修改")
        print("    - 评论区出现 AI 代码检视报告（含整体评估表格 + 问题清单）")
        print("    - 已有评论的问题标注「已有评论，跳过重复发布」")
        print("    - 新发现的问题在代码行有 inline comment")
        return True
    except Exception as e:
        print(f"  {FAIL} run_review_graph 异常: {e}")
        import traceback
        traceback.print_exc()
        return False


# ─────────────────────────────────────────────────────────────
# 入口
# ─────────────────────────────────────────────────────────────

async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-id", default="")
    parser.add_argument("--mr-iid", type=int, default=0)
    parser.add_argument("--real", action="store_true", help="执行真实 PR 验证（需要 --project-id 和 --mr-iid）")
    args = parser.parse_args()

    results: list[tuple[str, bool]] = []

    results.append(("[1] _parse_reported_keys",      check_parse_reported_keys()))
    results.append(("[2] _find_ai_summary_comment",  check_find_ai_summary_comment()))
    results.append(("[3] _build_ai_section",         check_build_ai_section()))
    results.append(("[4] publish_node e2e",          await check_publish_dedup()))

    if args.real:
        if not args.project_id or not args.mr_iid:
            print("\n[5] 跳过真实 PR 验证（需要 --project-id 和 --mr-iid）")
        else:
            results.append(("[5] 真实 PR 全流程", await check_real_pr(args.project_id, args.mr_iid)))

    # 汇总
    print(f"\n{'═'*60}")
    print("  验证结果汇总")
    print(f"{'═'*60}")
    all_pass = True
    for name, ok in results:
        mark = PASS if ok else FAIL
        print(f"  {mark}  {name}")
        if not ok:
            all_pass = False

    if all_pass:
        print(f"\n  所有验证通过！")
    else:
        print(f"\n  部分验证失败，请检查上方输出。")

    sys.exit(0 if all_pass else 1)


if __name__ == "__main__":
    asyncio.run(main())
