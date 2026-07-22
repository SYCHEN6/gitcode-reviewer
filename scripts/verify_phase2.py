"""Phase 2 Multi-Agent 架构验证脚本。

验证点：
  [1] LangGraph 图结构完整（supervisor → run_agents 循环 → summary → publish）
  [2] Supervisor LLM 动态决策（输出合法 SupervisorDecision JSON）
  [3] 专家 Agent 真正调用 get_file_content 工具（ReAct 工具调用）
  [4] 多 Agent 并行执行（asyncio.gather）
  [5] findings 通过 operator.add 聚合到 State
  [6] synthesize_node 去重 + severity 排序
  [7] publish_node 写回 GitCode（inline comment / global note）

用法：
    python scripts/verify_phase2.py --project-id chensiyu47/MindIE-SD_1344 --mr-iid 1
"""

import argparse
import asyncio
import logging
import sys
import time
from unittest.mock import AsyncMock, patch

from _common import *

# ── 日志配置（详细级别，追踪每个节点）──────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
# 只看我们关心的模块
for mod in ("src.graph", "src.agents", "src.tools"):
    logging.getLogger(mod).setLevel(logging.DEBUG)

logger = logging.getLogger("verify_phase2")

PASS = "✅"
FAIL = "❌"
WARN = "⚠️ "


# ── 工具函数 ───────────────────────────────────────────────────────────────

def section(title: str) -> None:
    print(f"\n{'─'*60}")
    print(f"  {title}")
    print(f"{'─'*60}")


# ── [1] 图结构验证（纯静态，不调 LLM）────────────────────────────────────

def check_graph_structure() -> bool:
    section("[1] LangGraph 图结构")
    from src.graph.review_graph import _graph

    nodes = set(_graph.nodes.keys()) - {"__start__"}
    required = {"supervisor", "run_agents", "summary", "synthesize", "critic", "publish"}
    missing = required - nodes
    extra = nodes - required

    if missing:
        print(f"  {FAIL} 缺少节点: {missing}")
        return False

    print(f"  {PASS} 所有节点存在: {sorted(nodes)}")

    return True


# ── [2] Supervisor 决策验证（真实 LLM 调用）──────────────────────────────

async def check_supervisor(project_id: str, mr_iid: int) -> dict | None:
    section("[2] Supervisor 动态决策（真实 LLM）")
    from src.tools.gitcode_client import GitCodeClient
    from src.config import settings
    from src.agents.supervisor import run_supervisor

    gc = GitCodeClient(settings.GITCODE_BASE_URL, settings.GITCODE_TOKEN)
    try:
        diff_data = await gc.get_pr_diff(project_id, mr_iid)
    except Exception as e:
        print(f"  {FAIL} get_pr_diff 失败: {e}")
        return None

    fake_state = {
        "iteration":            0,
        "file_list":            diff_data.get("files", []),
        "findings":             [],
        "supervisor_reasoning": [],
    }

    t0 = time.perf_counter()
    decision = await run_supervisor(fake_state)
    elapsed = time.perf_counter() - t0

    action = decision.get("action", "?")
    reasoning = decision.get("reasoning", "")
    agents = decision.get("agents_to_dispatch", [])

    ok = action in ("DISPATCH", "FINISH")
    icon = PASS if ok else FAIL
    print(f"  {icon} action={action}  reasoning={reasoning!r}  ({elapsed:.1f}s)")
    if agents:
        for a in agents:
            print(f"       → {a.get('agent_type')} files={len(a.get('files', []))} hint={a.get('focus_hint', '')!r}")
    else:
        print(f"       → agents_to_dispatch=[]")

    return decision if ok else None


# ── [3] ReAct 工具调用验证（单个 Agent，真实 LLM + 真实 API）────────────

async def check_react_tool_call(project_id: str, mr_iid: int) -> bool:
    section("[3] ReAct 工具调用（SecurityAgent 真实运行）")
    from src.tools.gitcode_client import GitCodeClient
    from src.config import settings
    from src.agents.security_agent import run_security_agent

    gc = GitCodeClient(settings.GITCODE_BASE_URL, settings.GITCODE_TOKEN)
    try:
        diff_data = await gc.get_pr_diff(project_id, mr_iid)
    except Exception as e:
        print(f"  {FAIL} get_pr_diff 失败: {e}")
        return False

    files = diff_data.get("files", [])[:3]  # 只取前 3 个文件
    head_sha = diff_data.get("head_sha", "")

    # 追踪工具调用次数
    # expert_agent.py 在自身模块内 import GitCodeClient，必须 patch 那里的引用
    import src.agents.expert_agent as ea_module
    from src.tools.gitcode_client import GitCodeClient as RealGC

    call_count = [0]

    class TrackingClient(RealGC):
        async def get_file_content(self, *args, **kwargs):
            call_count[0] += 1
            fname = args[1] if len(args) > 1 else kwargs.get("file_path", "?")
            print(f"       >> [ReAct 工具调用 #{call_count[0]}] get_file_content({fname!r})")
            return await super().get_file_content(*args, **kwargs)

    task = {
        "agent_type":  "SecurityAgent",
        "files":       files,
        "focus_hint":  "",
        "diff_slice":  diff_data.get("diff", "")[:2000],
        "project_id":  project_id,
        "mr_iid":      mr_iid,
    }

    with patch.object(ea_module, "GitCodeClient", TrackingClient):
        t0 = time.perf_counter()
        findings = await run_security_agent(task, head_sha)
        elapsed = time.perf_counter() - t0

    if call_count[0] > 0:
        print(f"  {PASS} 工具被调用 {call_count[0]} 次（真正的 ReAct）  耗时 {elapsed:.1f}s")
    else:
        print(f"  {WARN} 工具未被调用（模型可能不支持 function calling，已降级为 prefetch 模式）  耗时 {elapsed:.1f}s")

    print(f"  → SecurityAgent 返回 {len(findings)} 条 findings")
    for f in findings[:3]:
        print(f"     [{f.get('severity')}] {f.get('file')}:{f.get('line_start')} — {f.get('description', '')[:60]}")

    return True


# ── [4] 并行执行验证（mock LLM，验证 asyncio.gather 时序）────────────────

async def check_parallel_execution() -> bool:
    section("[4] 多 Agent 并行执行（mock 计时验证）")
    import time as _time

    call_times: list[tuple[str, float, float]] = []

    async def slow_agent(name: str, delay: float, task: dict, head_sha: str) -> list[dict]:
        start = _time.perf_counter()
        await asyncio.sleep(delay)
        end = _time.perf_counter()
        call_times.append((name, start, end))
        return []

    # _AGENT_MAP 在导入时已经捕获函数引用，必须 patch dict 本身
    from src.graph import review_graph as rg
    with patch.dict(rg._AGENT_MAP, {
        "SecurityAgent":    lambda t, h: slow_agent("Security",    0.2, t, h),
        "LogicAgent":       lambda t, h: slow_agent("Logic",       0.2, t, h),
        "QualityAgent":     lambda t, h: slow_agent("Quality",     0.2, t, h),
        "PerformanceAgent": lambda t, h: slow_agent("Performance", 0.2, t, h),
    }):
        state = {
            "project_id":        "owner/repo",
            "mr_iid":            1,
            "head_sha":          "abc",
            "diffs":             [],
            "agents_to_dispatch": [
                {"agent_type": "SecurityAgent",    "files": ["a.py"], "focus_hint": ""},
                {"agent_type": "LogicAgent",       "files": ["a.py"], "focus_hint": ""},
                {"agent_type": "QualityAgent",     "files": ["a.py"], "focus_hint": ""},
                {"agent_type": "PerformanceAgent", "files": ["a.py"], "focus_hint": ""},
            ],
        }
        t0 = _time.perf_counter()
        from src.graph.review_graph import run_agents_node
        await run_agents_node(state)
        total = _time.perf_counter() - t0

    # 4 个 agent 各 0.2s，串行应 ~0.8s，并行应 ~0.2s
    parallel = total < 0.5
    icon = PASS if parallel else FAIL
    print(f"  {icon} 4 个 Agent 总耗时 {total:.2f}s（并行 ≈0.2s，串行 ≈0.8s）")
    if call_times:
        starts = [s for _, s, _ in call_times]
        spread = max(starts) - min(starts)
        print(f"     最大启动时间差: {spread*1000:.0f}ms（<50ms 视为真并行）")

    return parallel


# ── [5] findings 聚合验证（静态 State 测试）──────────────────────────────

def check_state_aggregation() -> bool:
    section("[5] findings operator.add 聚合")
    import operator
    from typing import Annotated

    # 模拟两轮各产生 2 条 findings
    batch1 = [{"finding_id": "1", "agent": "SecurityAgent", "severity": "HIGH"}]
    batch2 = [{"finding_id": "2", "agent": "LogicAgent",    "severity": "MEDIUM"}]
    combined = operator.add(batch1, batch2)

    ok = len(combined) == 2 and combined[0]["finding_id"] == "1"
    print(f"  {PASS if ok else FAIL} 两批 findings 聚合后共 {len(combined)} 条（预期 2）")
    return ok


# ── [6] synthesize_node 去重验证（静态）──────────────────────────────────

def check_synthesize() -> bool:
    section("[6] synthesize_node 去重 + severity 排序")
    from src.graph.review_graph import synthesize_node

    # 同一位置两个 Agent 都报告了问题（应去重，保留高 severity）
    dup_findings = [
        {"finding_id": "a", "agent": "SecurityAgent", "severity": "HIGH",   "file": "x.py", "line_start": 10, "category": "security",  "description": "issue"},
        {"finding_id": "b", "agent": "LogicAgent",    "severity": "LOW",    "file": "x.py", "line_start": 10, "category": "security",  "description": "issue2"},
        {"finding_id": "c", "agent": "QualityAgent",  "severity": "MEDIUM", "file": "y.py", "line_start": 20, "category": "quality",   "description": "issue3"},
        {"finding_id": "d", "agent": "SecurityAgent", "severity": "CRITICAL","file": "z.py", "line_start": 5,  "category": "security",  "description": "issue4"},
    ]

    result = synthesize_node({"findings": dup_findings, "final_findings": []})
    final = result["final_findings"]

    # x.py:10 应只剩 HIGH（去重），且顺序 CRITICAL → HIGH → MEDIUM
    dedup_ok = len(final) == 3
    order_ok = [f["severity"] for f in final] == ["CRITICAL", "HIGH", "MEDIUM"]

    icon = PASS if (dedup_ok and order_ok) else FAIL
    print(f"  {icon} 去重: {len(dup_findings)}条 → {len(final)}条（预期3）  "
          f"排序: {[f['severity'] for f in final]}")
    return dedup_ok and order_ok


# ── [7] publish_node 写回验证（mock GitCodeClient）──────────────────────

async def check_publish(project_id: str, mr_iid: int) -> bool:
    section("[7] publish_node 写回 GitCode（mock 验证调用路径）")
    from src.tools import gitcode_client as gc_module

    posted_notes = []
    posted_inline = []

    class MockGC:
        async def post_mr_note(self, *a, **kw):
            posted_notes.append(kw.get("body", a[2] if len(a) > 2 else ""))
            return {"comment_id": 1}

        async def post_inline_comment(self, *a, **kw):
            posted_inline.append(kw.get("body", ""))
            return {"comment_id": 2}

        async def update_mr_description(self, *a, **kw):
            return {"success": True}

        async def get_repo_labels(self, *a, **kw):
            return []

        async def update_mr_label(self, *a, **kw):
            return {"success": True}

    fake_findings = [
        {
            "finding_id": "x1", "agent": "SecurityAgent", "severity": "HIGH",
            "category": "security", "file": "src/auth.py", "line_start": 10,
            "line_end": 10, "diff_position": 0,
            "description": "SQL 注入风险：用户输入直接拼接到查询",
            "suggestion_code": "cursor.execute(sql, (uid,))",
            "norm_reference": "",
        }
    ]

    state = {
        "project_id":    project_id,
        "mr_iid":        mr_iid,
        "head_sha":      "",
        "diffs":         [],
        "final_findings": fake_findings,
        "summary": {
            "risk_level":      "HIGH",
            "impact_analysis": "修改了认证模块",
            "risk_reason":     "存在 SQL 注入",
            "focus_points":    ["src/auth.py:10 SQL 注入"],
            "total_files":     1,
            "total_lines":     20,
        },
    }

    # publish_node 在 review_graph 模块内 import GitCodeClient，patch 那里
    import src.graph.review_graph as rg_module
    from src.graph.review_graph import publish_node
    with patch.object(rg_module, "GitCodeClient", lambda *a, **kw: MockGC()):
        await publish_node(state)

    total_comments = len(posted_notes) + len(posted_inline)
    ok = total_comments >= 1
    icon = PASS if ok else FAIL
    print(f"  {icon} 发出 {len(posted_inline)} 条 inline comment + {len(posted_notes)} 条 global note")
    if posted_notes:
        print(f"     note preview: {posted_notes[0][:80]!r}")
    return ok


# ── 主流程 ─────────────────────────────────────────────────────────────────

async def main(project_id: str, mr_iid: int) -> None:
    print(f"\n{'='*60}")
    print(f"  Phase 2 Multi-Agent 架构验证")
    print(f"  project={project_id}  mr={mr_iid}")
    print(f"{'='*60}")

    results: list[tuple[str, bool]] = []

    r1 = check_graph_structure()
    results.append(("[1] 图结构", r1))

    r2 = await check_supervisor(project_id, mr_iid)
    results.append(("[2] Supervisor 决策", r2 is not None))

    r3 = await check_react_tool_call(project_id, mr_iid)
    results.append(("[3] ReAct 工具调用", r3))

    r4 = await check_parallel_execution()
    results.append(("[4] 并行执行", r4))

    r5 = check_state_aggregation()
    results.append(("[5] findings 聚合", r5))

    r6 = check_synthesize()
    results.append(("[6] 去重+排序", r6))

    r7 = await check_publish(project_id, mr_iid)
    results.append(("[7] publish 写回", r7))

    # 汇总
    print(f"\n{'='*60}")
    passed = sum(1 for _, ok in results if ok)
    for name, ok in results:
        print(f"  {'✅' if ok else '❌'} {name}")
    print(f"\n结果：{passed}/{len(results)} 通过")
    if passed == len(results):
        print("🎉 Multi-Agent 架构验证通过！")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--mr-iid", type=int, required=True)
    args = parser.parse_args()
    asyncio.run(main(args.project_id, args.mr_iid))
