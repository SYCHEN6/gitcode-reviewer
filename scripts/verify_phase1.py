"""Phase 1 全量端到端验证脚本。

覆盖范围：
  [1] MCP Server 连通 & 工具注册
  [2] get_pr_diff      — 拉取 PR diff + SHA
  [3] get_file_content — 读取变更文件内容
  [4] post_mr_note     — 发送全局评论
  [5] post_inline_comment — 发送 inline comment
  [6] update_mr_description — 更新 MR 描述（追加，不覆盖）
  [7] update_mr_label  — 打风险标签

用法：
    # 终端 1：启动 MCP Server
    python -m src.mcp.gitcode_server

    # 终端 2：执行验证
    python scripts/verify_phase1.py --project-id "owner/repo" --mr-iid 1
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from src.config import settings

PASS = "✅"
FAIL = "❌"
SKIP = "⚠️ "


async def call(session: ClientSession, tool: str, args: dict) -> tuple[bool, dict | str]:
    result = await session.call_tool(tool, args)
    raw = result.content[0].text if result.content else ""
    if result.isError:
        return False, raw
    try:
        return True, json.loads(raw)
    except Exception:
        return True, raw


def _compute_diff_position(diff_text: str, filename: str) -> int | None:
    """计算 diff 中第一个 '+' 行的 position（从 @@ 后第 1 行算起）。"""
    in_file = False
    position = 0
    for line in diff_text.splitlines():
        if line.startswith(f"+++ b/{filename}") or line.startswith(f"+++ b/"):
            in_file = True
            continue
        if not in_file:
            continue
        if line.startswith("@@"):
            position = 0
            continue
        if line.startswith("--- "):
            continue
        position += 1
        if line.startswith("+") and not line.startswith("+++"):
            return position
    return None


async def verify(project_id: str, mr_iid: int) -> None:
    url = f"http://{settings.MCP_SERVER_HOST}:{settings.MCP_SERVER_PORT}/mcp"
    results: list[tuple[str, bool, str]] = []

    def log(step: str, ok: bool, detail: str = "") -> None:
        icon = PASS if ok else FAIL
        print(f"  {icon} {step}" + (f"  →  {detail}" if detail else ""))
        results.append((step, ok, detail))

    print(f"\n{'='*60}")
    print(f"Phase 1 验证  project={project_id}  mr={mr_iid}")
    print(f"{'='*60}\n")

    async with streamablehttp_client(url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()

            # [1] 工具注册
            tools_result = await session.list_tools()
            tool_names = [t.name for t in tools_result.tools]
            expected = {"get_pr_diff", "get_file_content", "post_inline_comment",
                        "post_suggestion", "post_mr_note",
                        "update_mr_description", "update_mr_label"}
            missing = expected - set(tool_names)
            log("[1] 工具注册", not missing,
                f"{len(tool_names)} 个工具" if not missing else f"缺少: {missing}")

            # [2] get_pr_diff
            ok, diff_data = await call(session, "get_pr_diff",
                                       {"project_id": project_id, "mr_iid": mr_iid})
            if ok and isinstance(diff_data, dict):
                files = diff_data.get("files", [])
                head_sha = diff_data.get("head_sha", "")
                log("[2] get_pr_diff", True,
                    f"{len(files)} 个文件  head={head_sha[:12]}...")
            else:
                log("[2] get_pr_diff", False, str(diff_data)[:120])
                print("\n❌ get_pr_diff 失败，后续步骤跳过")
                _print_summary(results)
                return

            # [3] get_file_content
            if files:
                target_file = files[0]
                ok, fc = await call(session, "get_file_content", {
                    "project_id": project_id,
                    "file_path": target_file,
                    "ref": head_sha,
                })
                if ok and isinstance(fc, dict) and fc.get("content"):
                    preview = fc["content"][:60].replace("\n", "\\n")
                    log("[3] get_file_content", True,
                        f"{target_file}  前60字符: {preview!r}")
                else:
                    log("[3] get_file_content", False, str(fc)[:120])
            else:
                print(f"  {SKIP} [3] get_file_content  跳过（无变更文件）")

            # [4] post_mr_note
            ok, note = await call(session, "post_mr_note", {
                "project_id": project_id,
                "mr_iid": mr_iid,
                "body": "🤖 **Phase 1 全量验证** — post_mr_note 正常",
            })
            log("[4] post_mr_note", ok and bool(note.get("comment_id") if isinstance(note, dict) else False),
                f"comment_id={note.get('comment_id') if isinstance(note, dict) else note}")

            # [5] post_inline_comment
            diff_text = diff_data.get("diff", "")
            pos = _compute_diff_position(diff_text, files[0]) if files else None
            if pos and head_sha and files:
                ok, ic = await call(session, "post_inline_comment", {
                    "project_id": project_id,
                    "mr_iid": mr_iid,
                    "body": "🤖 Phase 1 验证 — inline comment 测试",
                    "position": {
                        "head_sha": head_sha,
                        "new_path": files[0],
                        "new_line": pos,
                    },
                })
                if ok and isinstance(ic, dict):
                    log("[5] post_inline_comment", True,
                        f"comment_id={ic.get('comment_id')}  position={pos}")
                else:
                    # inline comment 失败不算整体失败（diff position 计算可能有偏差）
                    log("[5] post_inline_comment", False,
                        f"position={pos}  error={str(ic)[:100]}")
            else:
                print(f"  {SKIP} [5] post_inline_comment  跳过（无法计算 diff position）")

            # [6] update_mr_description（追加，不覆盖原有内容）
            ok, upd = await call(session, "update_mr_description", {
                "project_id": project_id,
                "mr_iid": mr_iid,
                "body": "<!-- AI review: Phase 1 验证通过 -->",
            })
            log("[6] update_mr_description", ok and isinstance(upd, dict) and upd.get("success"),
                str(upd))

            # [7] update_mr_label — 先查仓库已有标签，选第一个来测
            from src.tools.gitcode_client import GitCodeClient
            from src.config import settings as _s
            _gc = GitCodeClient(_s.GITCODE_BASE_URL, _s.GITCODE_TOKEN)
            existing_labels = await _gc.get_repo_labels(project_id)
            if existing_labels:
                test_label = existing_labels[0]["name"]
                ok, lbl = await call(session, "update_mr_label", {
                    "project_id": project_id,
                    "mr_iid": mr_iid,
                    "labels": [test_label],
                })
                log("[7] update_mr_label", ok and isinstance(lbl, dict) and lbl.get("success"),
                    f"label={test_label!r}  result={lbl}")
            else:
                print(f"  {SKIP} [7] update_mr_label  跳过（仓库无已有标签，Phase 2 再建）")

    _print_summary(results)


def _print_summary(results: list[tuple[str, bool, str]]) -> None:
    total = len(results)
    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n{'='*60}")
    print(f"结果：{passed}/{total} 通过")
    if passed == total:
        print("🎉 Phase 1 全量验证通过！")
    else:
        failed = [s for s, ok, _ in results if not ok]
        print(f"失败项：{', '.join(failed)}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Phase 1 全量端到端验证")
    parser.add_argument("--project-id", type=str, required=True,
                        help="'owner/repo' 格式，例如 chensiyu47/MindIE-SD_1344")
    parser.add_argument("--mr-iid", type=int, required=True)
    args = parser.parse_args()

    asyncio.run(verify(args.project_id, args.mr_iid))
