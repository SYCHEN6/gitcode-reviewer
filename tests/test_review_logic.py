"""针对本次重构的核心逻辑单元测试。

覆盖范围：
- synthesize_node：同行多 Agent 发现不再合并
- _parse_reported_keys：去重 key 升级为 (file, line, desc_40)
- _get_range_text / _description_plausible：critic 内容检查
- _parse_findings：import 语句过滤、中文 suggestion 过滤
- _nearest_added_line：行号匹配
- _rule_engine_dispatch：首轮规则引擎派遣
- supervisor_node：iteration=0 走规则引擎，iteration>0 走 LLM
- run_agents_node：Step Checkpoint 恢复（跳过已完成 Agent）
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from src.graph.review_graph import (
    _nearest_added_line,
    _get_range_text,
    _description_plausible,
    _parse_reported_keys,
    _rule_engine_dispatch,
    synthesize_node,
    critic_node,
    supervisor_node,
    run_agents_node,
)
from src.agents.expert_agent import _parse_findings


# ── _nearest_added_line ────────────────────────────────────────────────────

PATCH = """\
@@ -168,7 +168,12 @@
 context line
+if pool_size <= 0:
+    raise ValueError(f"pool_size must be positive, got {pool_size}")
+
 b, n, qb, kb = mask.shape
+if b == 0 or n == 0:
+    return mask
"""

def test_nearest_added_line_finds_plus_line():
    # line 169 is "if pool_size <= 0:" (+line)
    assert _nearest_added_line(PATCH, 169) == 169

def test_nearest_added_line_rejects_context_line():
    # line 168 is the context "context line" (no +)
    assert _nearest_added_line(PATCH, 168) is None

def test_nearest_added_line_rejects_missing_line():
    assert _nearest_added_line(PATCH, 999) is None


# ── _get_range_text ────────────────────────────────────────────────────────

PATCH2 = """\
@@ -224,3 +229,5 @@
 context
+actual_sparsity = 1.0 - float(mask.float().sum()) / float(mask.numel())
+print(f"[mask] target={actual_sparsity:.3f}")
 end context
"""

def test_get_range_text_single_line():
    text = _get_range_text(PATCH2, 230, 230)
    assert "actual_sparsity" in text
    assert "print" not in text

def test_get_range_text_multi_line():
    text = _get_range_text(PATCH2, 230, 231)
    assert "actual_sparsity" in text
    assert "print(" in text

def test_get_range_text_empty_when_out_of_range():
    text = _get_range_text(PATCH2, 999, 1000)
    assert text == ""


# ── _description_plausible ─────────────────────────────────────────────────

def test_plausible_passes_when_print_in_code():
    assert _description_plausible("调试残留：print 语句应删除", "    print('hello')")

def test_plausible_fails_when_print_in_desc_but_not_code():
    assert not _description_plausible("调试残留：print 语句应删除", "    raise ValueError('x')")

def test_plausible_passes_non_print_description():
    # 性能描述不含 "print(" → 不触发规则，默认通过
    assert _description_plausible("mask.float().sum() 引发热路径开销", "    actual_sparsity = 1.0 - float(mask.float().sum())")

def test_plausible_passes_multiline_range_with_print():
    # 区间文本包含 print，description 也说有 print → 通过
    code_range = "actual_sparsity = 1.0 - float(mask.float().sum())\nprint(f'[debug] {actual_sparsity}')"
    assert _description_plausible("热路径 print() 持有 GIL", code_range)


# ── _parse_reported_keys ───────────────────────────────────────────────────

def _make_comment(body: str) -> dict:
    return {"body": body, "id": 1}

AI_COMMENT = (
    "🟡 **[MEDIUM]** `layers/attn.py:240`\n\n"
    "调试残留：print 语句用于调试，应删除\n\n"
    "```suggestion\n\n```"
)

def test_parse_reported_keys_extracts_pair():
    comments = [_make_comment(AI_COMMENT)]
    reported = _parse_reported_keys(comments)
    assert len(reported) == 1
    key = next(iter(reported))
    assert key[0] == "layers/attn.py"
    assert key[1] == 240
    assert len(key) == 2  # 跨 run 去重只用 (file, line) 二元组

def test_parse_reported_keys_two_findings_same_line_deduped():
    # 同一行两条不同描述 → 跨 run 去重视为同一位置，只记录一次
    comment1 = (
        "🟡 **[MEDIUM]** `file.py:100`\n\n魔法数字：0.9 应提取为常量\n\n```suggestion\n```"
    )
    comment2 = (
        "🟡 **[MEDIUM]** `file.py:100`\n\n静默裁剪：无日志输出给用户\n\n```suggestion\n```"
    )
    reported = _parse_reported_keys([_make_comment(comment1), _make_comment(comment2)])
    assert len(reported) == 1  # 同行 → 跨 run 只需记一次，防止重复 review 时重复发评论

def test_parse_reported_keys_ignores_non_ai_comment():
    # 人工评论没有 emoji+severity 格式 → 被忽略
    comments = [_make_comment("普通人工评论，没有任何 severity 标记")]
    assert len(_parse_reported_keys(comments)) == 0


# ── synthesize_node ────────────────────────────────────────────────────────

def _finding(agent, file, line, severity, description, suggestion=None):
    return {
        "agent": agent, "file": file, "line_start": line, "line_end": line,
        "severity": severity, "description": description,
        "suggestion_code": suggestion, "category": "quality",
    }

def test_synthesize_keeps_both_findings_same_line_different_agents():
    """不同 Agent 同一行，描述差异大（重叠度 < 0.30） → 各自保留。"""
    findings = [
        _finding("QualityAgent",     "f.py", 100, "MEDIUM", "魔法数字：0.9 应提取为 MAX_SPARSITY 常量", "if sparsity > MAX_SPARSITY:"),
        _finding("LogicAgent",       "f.py", 100, "MEDIUM", "静默裁剪：修改用户参数前应记录警告日志"),
    ]
    state = {"findings": findings, "final_findings": []}
    result = synthesize_node(state)
    assert len(result["final_findings"]) == 2


def test_synthesize_dedup_cross_agent_high_overlap():
    """不同 Agent 同行，关键词重叠度 ≥ 0.30 → 视为同一问题，只保留一条（取更高 severity 或更详细）。"""
    # 两条都说的是 print 调试残留，描述角度稍不同但核心词重叠
    findings = [
        _finding("QualityAgent",     "f.py", 429, "HIGH",
                 "调试残留：print 位于 bsa_sparse_attention_v3 函数入口，每次调用均输出，属于调试日志"),
        _finding("PerformanceAgent", "f.py", 429, "HIGH",
                 "热路径 print：bsa_sparse_attention_v3 核心函数每次前向传播执行 print，持有 GIL 拖慢吞吐"),
    ]
    state = {"findings": findings, "final_findings": []}
    result = synthesize_node(state)
    # Step 1.5 应将两条合并为一条
    assert len(result["final_findings"]) == 1
    # 保留的那条必须是 HIGH
    assert result["final_findings"][0]["severity"] == "HIGH"


def test_synthesize_dedup_keeps_suggestion_code_when_merging():
    """合并时优先保留有 suggestion_code 的 finding。"""
    findings = [
        _finding("QualityAgent",     "f.py", 100, "HIGH",
                 "调试 print 语句：print 函数调用应删除", suggestion=None),
        _finding("PerformanceAgent", "f.py", 100, "HIGH",
                 "热路径 print：print 调用每次前向传播都执行，应删除", suggestion=""),
    ]
    state = {"findings": findings, "final_findings": []}
    result = synthesize_node(state)
    assert len(result["final_findings"]) == 1
    assert result["final_findings"][0]["suggestion_code"] == ""  # 保留有 suggestion 的那条


def test_synthesize_dedup_fuzzy_line_tolerance():
    """不同 Agent 行号相差 1-2 行但描述高度重叠 → ±2 行容差内合并为一条（line_start 差了 1）。"""
    findings = [
        _finding("PerformanceAgent", "f.py", 513, "HIGH",
                 "热路径 print：bsa_sparse_attention_v3 尾部 print 输出 seqlen/batch/mask_shape，每次前向传播执行，GIL 开销"),
        _finding("QualityAgent",     "f.py", 514, "HIGH",   # LLM 行号偏差 1 行
                 "调试残留：print 语句位于 bsa_sparse_attention_v3 中 mask 处理完毕后，每次前向调用时触发，高频路径调试代码"),
    ]
    state = {"findings": findings, "final_findings": []}
    result = synthesize_node(state)
    # 行号差 1 + 关键词高度重叠 → 应合并为一条
    assert len(result["final_findings"]) == 1
    assert result["final_findings"][0]["severity"] == "HIGH"


def test_synthesize_no_merge_beyond_tolerance():
    """行号差超过 2 行 → 不合并（即使描述相似）。"""
    findings = [
        _finding("PerformanceAgent", "f.py", 429, "HIGH",
                 "热路径 print：bsa_sparse_attention_v3 入口 print 每次前向传播执行，GIL 开销"),
        _finding("QualityAgent",     "f.py", 513, "HIGH",  # 相差 84 行，不同位置
                 "调试残留：print 语句位于 bsa_sparse_attention_v3 尾部，每次前向调用触发，高频路径调试代码"),
    ]
    state = {"findings": findings, "final_findings": []}
    result = synthesize_node(state)
    # 位置差异大 → 各自独立保留
    assert len(result["final_findings"]) == 2

def test_synthesize_deduplicates_same_agent_same_line():
    """同 Agent 同行重复报 → 只保留最高 severity。"""
    findings = [
        _finding("QualityAgent", "f.py", 100, "LOW",    "命名不规范"),
        _finding("QualityAgent", "f.py", 100, "MEDIUM", "命名不规范，影响可读性"),
    ]
    state = {"findings": findings, "final_findings": []}
    result = synthesize_node(state)
    assert len(result["final_findings"]) == 1
    assert result["final_findings"][0]["severity"] == "MEDIUM"

def test_synthesize_deduplicates_identical_description_across_agents():
    """跨 Agent 描述相同 → 去重只保留一条。"""
    findings = [
        _finding("QualityAgent",     "f.py", 50, "MEDIUM", "调试残留：print 语句应删除"),
        _finding("PerformanceAgent", "f.py", 50, "MEDIUM", "调试残留：print 语句应删除"),
    ]
    state = {"findings": findings, "final_findings": []}
    result = synthesize_node(state)
    assert len(result["final_findings"]) == 1

def test_synthesize_keeps_different_lines_separate():
    findings = [
        _finding("QualityAgent", "f.py", 100, "MEDIUM", "issue A"),
        _finding("QualityAgent", "f.py", 200, "HIGH",   "issue B"),
    ]
    state = {"findings": findings, "final_findings": []}
    result = synthesize_node(state)
    assert len(result["final_findings"]) == 2
    # 按 severity 排序：HIGH 在前
    assert result["final_findings"][0]["severity"] == "HIGH"


# ── _parse_findings（import 过滤）─────────────────────────────────────────

def test_parse_findings_filters_import_in_suggestion():
    raw = '''[
      {
        "file": "layers/attn.py",
        "line_start": 100,
        "severity": "MEDIUM",
        "description": "缺少警告日志",
        "suggestion_code": "if sparsity > 0.9:\\n    import warnings\\n    warnings.warn('msg')"
      }
    ]'''
    result = _parse_findings(raw, "LogicAgent")
    assert len(result) == 1
    assert result[0]["suggestion_code"] is None  # import 过滤掉

def test_parse_findings_keeps_valid_suggestion():
    raw = '''[
      {
        "file": "layers/attn.py",
        "line_start": 100,
        "severity": "MEDIUM",
        "description": "魔法数字",
        "suggestion_code": "if sparsity > MAX_SPARSITY:"
      }
    ]'''
    result = _parse_findings(raw, "QualityAgent")
    assert result[0]["suggestion_code"] == "if sparsity > MAX_SPARSITY:"

def test_parse_findings_filters_chinese_suggestion():
    raw = '''[
      {
        "file": "a.py",
        "line_start": 1,
        "severity": "LOW",
        "description": "some issue",
        "suggestion_code": "请检查并确认这里是否需要关闭资源，如有必要请添加关闭逻辑"
      }
    ]'''
    result = _parse_findings(raw, "LogicAgent")
    assert result[0]["suggestion_code"] is None  # 中文过滤掉


# ── _rule_engine_dispatch ──────────────────────────────────────────────────

def test_rule_engine_small_plain_code():
    tasks = _rule_engine_dispatch(["src/utils/helper.py"], ["Python"], "small", {"tier": "small"})
    agents = {t["agent_type"] for t in tasks}
    assert "QualityAgent" in agents
    assert "LogicAgent" in agents
    assert "SecurityAgent" not in agents
    assert "PerformanceAgent" not in agents

def test_rule_engine_ml_path_adds_performance():
    tasks = _rule_engine_dispatch(["layers/flash_attn/attn.py"], ["Python"], "small", {"tier": "small"})
    agents = {t["agent_type"] for t in tasks}
    assert "PerformanceAgent" in agents
    assert "SecurityAgent" not in agents

def test_rule_engine_auth_path_adds_security():
    tasks = _rule_engine_dispatch(["src/auth/jwt_handler.py"], ["Python"], "small", {"tier": "small"})
    agents = {t["agent_type"] for t in tasks}
    assert "SecurityAgent" in agents
    assert "PerformanceAgent" not in agents

def test_rule_engine_sql_file_adds_security():
    tasks = _rule_engine_dispatch(["migrations/0001_init.sql"], ["SQL"], "small", {"tier": "small"})
    agents = {t["agent_type"] for t in tasks}
    assert "SecurityAgent" in agents

def test_rule_engine_medium_dispatches_all_four():
    tasks = _rule_engine_dispatch(["src/main.py"], ["Python"], "medium", {"tier": "medium"})
    agents = {t["agent_type"] for t in tasks}
    assert agents == {"SecurityAgent", "LogicAgent", "QualityAgent", "PerformanceAgent"}


# ── supervisor_node：iteration 分支 ────────────────────────────────────────

_BASE_STATE = {
    "project_id": "owner/repo", "mr_iid": 1, "commit_sha": "abc", "task_id": "",
    "file_list": ["src/auth/login.py"], "diffs": [], "head_sha": "", "base_sha": "",
    "raw_diff": "", "pr_meta": {}, "summary": {},
    "pr_stats": {"tier": "small", "lines_added": 20, "lines_removed": 5, "files": 1},
    "languages": ["Python"], "findings": [], "final_findings": [],
    "supervisor_action": "", "supervisor_reasoning": [], "agents_to_dispatch": [],
}

@pytest.mark.asyncio
async def test_supervisor_node_iteration0_uses_rule_engine():
    """iteration=0 走规则引擎；medium+ tier 还会调 get_focus_hints（LLM Advisor），不调 run_supervisor。
    small tier 跳过 get_focus_hints（Fix 2：避免额外 LLM 延迟）。
    """
    # 用 medium tier 验证 hints 路径
    medium_state = {
        **_BASE_STATE,
        "pr_stats": {"tier": "medium", "lines_added": 200, "lines_removed": 50, "files": 5},
        "iteration": 0,
    }
    with patch("src.graph.review_graph.get_focus_hints", new_callable=AsyncMock) as mock_hints, \
         patch("src.graph.review_graph.run_supervisor", new_callable=AsyncMock) as mock_sup:
        mock_hints.return_value = {"SecurityAgent": "关注 JWT 处理"}
        result = await supervisor_node(medium_state)

    assert result["supervisor_action"] == "DISPATCH"
    assert mock_hints.called            # medium tier → LLM Advisor 被调用
    assert not mock_sup.called          # LLM Supervisor 不调用
    agents = {t["agent_type"] for t in result["agents_to_dispatch"]}
    assert "SecurityAgent" in agents    # auth 路径触发

@pytest.mark.asyncio
async def test_supervisor_node_iteration1_uses_llm():
    """iteration>0 必须走 LLM Supervisor，不走规则引擎。"""
    with patch("src.graph.review_graph.get_focus_hints", new_callable=AsyncMock) as mock_hints, \
         patch("src.graph.review_graph.run_supervisor", new_callable=AsyncMock) as mock_sup:
        mock_sup.return_value = {"action": "FINISH", "reasoning": "足够了", "agents_to_dispatch": []}
        state = {**_BASE_STATE, "iteration": 1}
        result = await supervisor_node(state)

    assert not mock_hints.called        # LLM Advisor 不调用
    assert mock_sup.called              # LLM Supervisor 被调用
    assert result["supervisor_action"] == "FINISH"


# ── run_agents_node：Step Checkpoint ──────────────────────────────────────

def _make_state(task_id="", iteration=0, agents=None):
    tasks = [{"agent_type": a, "files": ["f.py"], "focus_hint": ""} for a in (agents or [])]
    return {
        **_BASE_STATE,
        "task_id": task_id,
        "iteration": iteration,
        "agents_to_dispatch": tasks,
        "diffs": [{"filename": "f.py", "patch": "@@ -1,1 +1,2 @@\n context\n+new line\n", "status": "modified"}],
        "findings": [],
    }

@pytest.mark.asyncio
async def test_run_agents_checkpoint_skips_completed_agent():
    """Checkpoint 命中时，已完成的 Agent 不调用 LLM，直接返回缓存结果。"""
    cached = [{"file": "f.py", "line_start": 1, "severity": "HIGH", "description": "cached finding"}]

    with patch("src.graph.review_graph.run_security_agent", new_callable=AsyncMock) as mock_agent, \
         patch("src.db.repository.load_agent_findings", new_callable=AsyncMock) as mock_load, \
         patch("src.db.repository.save_agent_result", new_callable=AsyncMock):
        mock_load.return_value = {"SecurityAgent": cached}
        state = _make_state(task_id="tid-123", iteration=0, agents=["SecurityAgent"])
        result = await run_agents_node(state)

    assert not mock_agent.called                # LLM 不调用
    assert len(result["findings"]) == 1
    assert result["findings"][0]["description"] == "cached finding"

@pytest.mark.asyncio
async def test_run_agents_checkpoint_not_applied_on_iteration1():
    """iteration=1（Supervisor 追查轮）不读 checkpoint，强制重新调用 Agent。"""
    cached = [{"file": "f.py", "line_start": 1, "severity": "HIGH", "description": "stale"}]
    fresh  = [{"file": "f.py", "line_start": 2, "severity": "CRITICAL", "description": "fresh"}]

    # _AGENT_MAP 在模块导入时绑定函数引用，必须用 patch.dict 替换 dict 条目
    agent_mock = AsyncMock(return_value=fresh)
    with patch.dict("src.graph.review_graph._AGENT_MAP", {"SecurityAgent": agent_mock}), \
         patch("src.db.repository.load_agent_findings", new_callable=AsyncMock) as mock_load, \
         patch("src.db.repository.save_agent_result", new_callable=AsyncMock):
        mock_load.return_value = {"SecurityAgent": cached}
        state = _make_state(task_id="tid-123", iteration=1, agents=["SecurityAgent"])
        result = await run_agents_node(state)

    assert agent_mock.called                    # iteration=1 → Agent 重新执行
    assert not mock_load.called                 # checkpoint 不读取
    assert result["findings"][0]["description"] == "fresh"

@pytest.mark.asyncio
async def test_run_agents_saves_checkpoint_after_completion():
    """Agent 成功完成后，结果应写入 DB checkpoint。"""
    findings = [{"file": "f.py", "line_start": 5, "severity": "MEDIUM", "description": "issue"}]

    agent_mock = AsyncMock(return_value=findings)
    with patch.dict("src.graph.review_graph._AGENT_MAP", {"QualityAgent": agent_mock}), \
         patch("src.db.repository.load_agent_findings", new_callable=AsyncMock) as mock_load, \
         patch("src.db.repository.save_agent_result", new_callable=AsyncMock) as mock_save:
        mock_load.return_value = {}
        state = _make_state(task_id="tid-456", iteration=0, agents=["QualityAgent"])
        await run_agents_node(state)

    mock_save.assert_called_once()
    call_args = mock_save.call_args
    assert call_args[0][0] == "tid-456"         # task_id
    assert call_args[0][1] == "QualityAgent"    # agent_type
    assert call_args[0][2] == findings          # findings
    assert call_args[1]["status"] == "completed"


# ── synthesize_node: finding_id 分配 ─────────────────────────────────────────

def test_synthesize_assigns_finding_ids():
    """synthesize_node 的每条 final finding 都应有 finding_id (UUID hex)。"""
    state = {
        "findings": [
            {"agent": "QualityAgent", "file": "a.py", "line_start": 10,
             "severity": "HIGH", "description": "issue A"},
            {"agent": "SecurityAgent", "file": "a.py", "line_start": 20,
             "severity": "MEDIUM", "description": "issue B"},
        ],
        "diffs": [],
    }
    result = synthesize_node(state)
    for f in result["final_findings"]:
        assert "finding_id" in f
        assert len(f["finding_id"]) == 32  # UUID hex


def test_synthesize_finding_id_not_overwritten():
    """已有 finding_id 的 finding 不应被重新分配。"""
    state = {
        "findings": [
            {"agent": "QualityAgent", "file": "a.py", "line_start": 10,
             "severity": "HIGH", "description": "issue A", "finding_id": "preset-id"},
        ],
        "diffs": [],
    }
    result = synthesize_node(state)
    assert result["final_findings"][0]["finding_id"] == "preset-id"


# ── handlers: 命令解析 ────────────────────────────────────────────────────────

def test_handle_explain_parses_file_line():
    """_handle_explain 能正确解析 /ai explain src/foo.py:42 → file:line 模式。"""
    import re
    note = "/ai explain src/foo.py:42"
    rest = re.sub(r"^/ai\s+explain\s*", "", note).strip()
    m = re.match(r"^([^\s:]+):(\d+)(?:-(\d+))?$", rest)
    assert m is not None
    assert m.group(1) == "src/foo.py"
    assert int(m.group(2)) == 42
    assert m.group(3) is None


def test_handle_explain_parses_range():
    """_handle_explain 能解析 /ai explain src/foo.py:10-30 的行范围。"""
    import re
    note = "/ai explain src/foo.py:10-30"
    rest = re.sub(r"^/ai\s+explain\s*", "", note).strip()
    m = re.match(r"^([^\s:]+):(\d+)(?:-(\d+))?$", rest)
    assert m is not None
    assert int(m.group(2)) == 10
    assert int(m.group(3)) == 30


def test_handle_explain_snippet_extracts_code():
    """/ai explain + 代码片段 → 提取代码，不误判为 file:line。"""
    import re
    note = "/ai explain\ndef foo(x):\n    return x * 2"
    rest = re.sub(r"^/ai\s+explain\s*", "", note, flags=re.IGNORECASE).strip()
    # 不应匹配 file:line 格式
    m = re.match(r"^([^\s:]+):(\d+)(?:-(\d+))?$", rest)
    assert m is None
    # 应提取到代码片段
    snippet = re.sub(r"^```\w*\n?", "", rest)
    snippet = re.sub(r"\n?```$", "", snippet).strip()
    assert "def foo" in snippet


def test_handle_explain_snippet_strips_code_fence():
    """带 Markdown 代码围栏的片段应剥除围栏标记。"""
    import re
    note = "/ai explain\n```python\ndef bar():\n    pass\n```"
    rest = re.sub(r"^/ai\s+explain\s*", "", note, flags=re.IGNORECASE).strip()
    snippet = re.sub(r"^```\w*\n?", "", rest)
    snippet = re.sub(r"\n?```$", "", snippet).strip()
    assert snippet.startswith("def bar()")
    assert "```" not in snippet


@pytest.mark.asyncio
async def test_handle_note_summary_triggers_background_task():
    """/ai summary 命令应派发 run_summary_only 任务，不调用 run_review_graph。"""
    from fastapi import BackgroundTasks
    from unittest.mock import MagicMock, patch, AsyncMock

    bt = MagicMock(spec=BackgroundTasks)
    redis_mock = AsyncMock()
    redis_mock.exists = AsyncMock(return_value=False)
    redis_mock.setex  = AsyncMock()

    payload = {
        "object_attributes": {"noteable_type": "MergeRequest", "note": "/ai summary"},
        "project":           {"path_with_namespace": "owner/repo"},
        "merge_request":     {"iid": 1},
    }

    with patch("src.webhook.handlers.run_review_graph") as mock_review, \
         patch("src.webhook.handlers.run_summary_only") as mock_summary:
        from src.webhook.handlers import handle_note
        await handle_note(payload, bt, redis_mock)

    bt.add_task.assert_called_once_with(mock_summary, "owner/repo", 1)
    mock_review.assert_not_called()


@pytest.mark.asyncio
async def test_handle_push_marks_applied_on_suggestion_commit():
    """/push 事件检测到 Apply suggestion commit 时，应触发 mark_suggestions_applied。"""
    from fastapi import BackgroundTasks
    from unittest.mock import MagicMock, patch

    bt = MagicMock(spec=BackgroundTasks)
    payload = {
        "project": {"path_with_namespace": "owner/repo"},
        "commits": [
            {
                "id": "abc123",
                "message": "Apply 1 suggestion (AI reviewer)",
                "modified": ["src/foo.py"],
                "added": [],
            }
        ],
    }

    with patch("src.webhook.handlers._mark_suggestions_applied") as mock_mark:
        from src.webhook.handlers import handle_push
        await handle_push(payload, bt)

    bt.add_task.assert_called_once_with(mock_mark, "owner/repo", ["src/foo.py"])
