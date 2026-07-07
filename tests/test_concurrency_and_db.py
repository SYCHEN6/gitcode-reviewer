"""单元测试：分布式锁 / 全局信号量 / DB repository / per-project 配置。

覆盖范围：
- _distributed_mr_lock：锁获取 + Lua 安全释放 + 超时降级
- _distributed_global_semaphore：信号量获取 + 上限保护 + 释放
- repository.create_or_get_task：幂等创建
- repository.load_agent_findings：只返回 status=completed 的条目
- repository.save_agent_result：INSERT OR UPDATE 幂等
- repository.save_suggestion + mark_suggestions_applied
- project_config：filter_findings_by_config
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# ─────────────────────────────────────────────────────────────────────────────
# 分布式 MR 锁
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_mr_lock_acquired_and_released():
    """锁可正常获取并在 with 块结束后通过 Lua 脚本释放。"""
    mock_redis = AsyncMock()
    mock_redis.set = AsyncMock(return_value=True)      # SET NX 成功
    mock_redis.eval = AsyncMock(return_value=1)        # Lua 释放成功

    from src.graph.review_graph import _distributed_mr_lock
    with patch("src.graph.review_graph._get_redis", return_value=mock_redis):
        async with _distributed_mr_lock("owner/repo", 1):
            pass

    mock_redis.set.assert_called_once()
    # 退出时调用 Lua 释放脚本
    mock_redis.eval.assert_called_once()


@pytest.mark.asyncio
async def test_mr_lock_not_released_when_not_acquired():
    """锁未获取时（超时降级），退出 with 块不调用 Lua 释放。"""
    mock_redis = AsyncMock()
    mock_redis.set = AsyncMock(return_value=False)  # SET NX 持续失败
    mock_redis.eval = AsyncMock()

    from src.graph.review_graph import _distributed_mr_lock
    # 超时设为 0 秒，使循环立即退出
    with patch("src.graph.review_graph._get_redis", return_value=mock_redis):
        async with _distributed_mr_lock("owner/repo", 1, timeout_seconds=0):
            pass

    mock_redis.eval.assert_not_called()  # 未获取锁，不释放


@pytest.mark.asyncio
async def test_mr_lock_key_format():
    """锁 key 格式必须是 review:lock:{project_id}:{mr_iid}。"""
    mock_redis = AsyncMock()
    mock_redis.set = AsyncMock(return_value=True)
    mock_redis.eval = AsyncMock(return_value=1)

    from src.graph.review_graph import _distributed_mr_lock
    with patch("src.graph.review_graph._get_redis", return_value=mock_redis):
        async with _distributed_mr_lock("owner/repo", 42):
            pass

    call_kwargs = mock_redis.set.call_args
    lock_key = call_kwargs[0][0]  # 第一个位置参数
    assert lock_key == "review:lock:owner/repo:42"


@pytest.mark.asyncio
async def test_mr_lock_released_even_on_exception():
    """with 块内抛出异常时，锁仍应被释放（finally 保证）。"""
    mock_redis = AsyncMock()
    mock_redis.set = AsyncMock(return_value=True)
    mock_redis.eval = AsyncMock(return_value=1)

    from src.graph.review_graph import _distributed_mr_lock
    with patch("src.graph.review_graph._get_redis", return_value=mock_redis):
        try:
            async with _distributed_mr_lock("owner/repo", 1):
                raise RuntimeError("simulated error")
        except RuntimeError:
            pass

    mock_redis.eval.assert_called_once()  # finally 块释放锁


# ─────────────────────────────────────────────────────────────────────────────
# 分布式全局信号量
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_semaphore_acquired_and_decremented():
    """信号量正常获取后，退出时自动 DECR。"""
    mock_redis = AsyncMock()
    mock_redis.eval = AsyncMock(return_value=1)    # Lua INCR+检查：未超限，返回计数
    mock_redis.decr = AsyncMock()

    from src.graph.review_graph import _distributed_global_semaphore
    with patch("src.graph.review_graph._get_redis", return_value=mock_redis):
        async with _distributed_global_semaphore(max_count=10):
            pass

    mock_redis.eval.assert_called_once()
    mock_redis.decr.assert_called_once()


@pytest.mark.asyncio
async def test_semaphore_not_decremented_on_timeout():
    """信号量未获取时（超时降级），不调用 DECR。"""
    mock_redis = AsyncMock()
    mock_redis.eval = AsyncMock(return_value=0)  # Lua：超限，返回 0
    mock_redis.decr = AsyncMock()

    from src.graph.review_graph import _distributed_global_semaphore
    with patch("src.graph.review_graph._get_redis", return_value=mock_redis):
        async with _distributed_global_semaphore(max_count=1, timeout_seconds=0):
            pass

    mock_redis.decr.assert_not_called()


@pytest.mark.asyncio
async def test_semaphore_decremented_on_exception():
    """with 块内抛出异常时，信号量仍应被释放。"""
    mock_redis = AsyncMock()
    mock_redis.eval = AsyncMock(return_value=2)
    mock_redis.decr = AsyncMock()

    from src.graph.review_graph import _distributed_global_semaphore
    with patch("src.graph.review_graph._get_redis", return_value=mock_redis):
        try:
            async with _distributed_global_semaphore(max_count=10):
                raise ValueError("test error")
        except ValueError:
            pass

    mock_redis.decr.assert_called_once()


# ─────────────────────────────────────────────────────────────────────────────
# DB repository
# ─────────────────────────────────────────────────────────────────────────────

def _make_mock_session(fetchone=None, fetchall=None):
    """构造一个 mock AsyncSession（同步 context manager）。"""
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)

    result_mock = MagicMock()
    result_mock.fetchone = MagicMock(return_value=fetchone)
    result_mock.fetchall = MagicMock(return_value=fetchall or [])

    session.execute = AsyncMock(return_value=result_mock)
    session.commit = AsyncMock()
    return session


@pytest.mark.asyncio
async def test_create_or_get_task_returns_existing():
    """任务已存在时，返回已有 task_id，不插入新记录。"""
    session = _make_mock_session(fetchone=("existing-task-id",))
    session_factory = MagicMock()
    session_factory.return_value = session

    with patch("src.db.repository._get_engine"), \
         patch("src.db.repository._SessionLocal", session_factory), \
         patch("src.db.repository._tables_ready", True):
        from src.db.repository import create_or_get_task
        task_id = await create_or_get_task("owner/repo", 1, "abc123")

    assert task_id == "existing-task-id"
    # 只查询一次，不 INSERT
    assert session.execute.call_count == 1


@pytest.mark.asyncio
async def test_create_or_get_task_creates_new():
    """任务不存在时，生成新 task_id 并插入。"""
    session = _make_mock_session(fetchone=None)
    session_factory = MagicMock()
    session_factory.return_value = session

    with patch("src.db.repository._get_engine"), \
         patch("src.db.repository._SessionLocal", session_factory), \
         patch("src.db.repository._tables_ready", True):
        from src.db.repository import create_or_get_task
        task_id = await create_or_get_task("owner/repo", 1, "newsha")

    assert len(task_id) == 32  # UUID hex
    assert session.execute.call_count == 2  # SELECT + INSERT
    session.commit.assert_called_once()


@pytest.mark.asyncio
async def test_load_agent_findings_parses_json():
    """load_agent_findings 正确解析 JSON 字段，返回 {agent_type: list}。"""
    import json as _json
    findings = [{"file": "a.py", "line_start": 10, "severity": "HIGH", "description": "issue"}]
    rows = [("SecurityAgent", _json.dumps(findings))]

    session = _make_mock_session(fetchall=rows)
    session_factory = MagicMock()
    session_factory.return_value = session

    with patch("src.db.repository._get_engine"), \
         patch("src.db.repository._SessionLocal", session_factory):
        from src.db.repository import load_agent_findings
        result = await load_agent_findings("task-123")

    assert "SecurityAgent" in result
    assert result["SecurityAgent"][0]["severity"] == "HIGH"


@pytest.mark.asyncio
async def test_load_agent_findings_handles_invalid_json():
    """findings_json 为 None 或损坏时，返回空列表而不是抛异常。"""
    rows = [("QualityAgent", None), ("LogicAgent", "{broken")]

    session = _make_mock_session(fetchall=rows)
    session_factory = MagicMock()
    session_factory.return_value = session

    with patch("src.db.repository._get_engine"), \
         patch("src.db.repository._SessionLocal", session_factory):
        from src.db.repository import load_agent_findings
        result = await load_agent_findings("task-456")

    assert result["QualityAgent"] == []
    assert result["LogicAgent"] == []


@pytest.mark.asyncio
async def test_save_agent_result_commits():
    """save_agent_result 调用 INSERT ... ON DUPLICATE KEY UPDATE，然后 commit。"""
    session = _make_mock_session()
    session_factory = MagicMock()
    session_factory.return_value = session

    with patch("src.db.repository._get_engine"), \
         patch("src.db.repository._SessionLocal", session_factory):
        from src.db.repository import save_agent_result
        await save_agent_result(
            "task-789", "SecurityAgent",
            [{"file": "f.py", "line_start": 1, "severity": "HIGH", "description": "x"}],
            tokens_in=100, tokens_out=50, duration_ms=3000,
        )

    session.execute.assert_called_once()
    session.commit.assert_called_once()
    # 验证 INSERT ON DUPLICATE KEY UPDATE 语句
    sql = str(session.execute.call_args[0][0])
    assert "INSERT" in sql.upper()
    assert "DUPLICATE" in sql.upper() or "UPDATE" in sql.upper()


@pytest.mark.asyncio
async def test_save_suggestion_inserts():
    """save_suggestion 正确构造 INSERT IGNORE 语句。"""
    session = _make_mock_session()
    session_factory = MagicMock()
    session_factory.return_value = session

    with patch("src.db.repository._get_engine"), \
         patch("src.db.repository._SessionLocal", session_factory):
        from src.db.repository import save_suggestion
        await save_suggestion(
            "task-id", "finding-uuid", "owner/repo", 1,
            comment_id=12345, file_path="src/foo.py", line_start=42,
        )

    session.execute.assert_called_once()
    session.commit.assert_called_once()
    sql = str(session.execute.call_args[0][0])
    assert "INSERT" in sql.upper()


@pytest.mark.asyncio
async def test_mark_suggestions_applied_updates():
    """mark_suggestions_applied 返回受影响行数（通过 rowcount mock）。"""
    result_mock = MagicMock()
    result_mock.rowcount = 2

    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.execute = AsyncMock(return_value=result_mock)
    session.commit = AsyncMock()

    session_factory = MagicMock()
    session_factory.return_value = session

    with patch("src.db.repository._get_engine"), \
         patch("src.db.repository._SessionLocal", session_factory):
        from src.db.repository import mark_suggestions_applied
        count = await mark_suggestions_applied("owner/repo", ["src/foo.py", "src/bar.py"])

    assert count == 2
    session.execute.assert_called_once()
    session.commit.assert_called_once()


# ─────────────────────────────────────────────────────────────────────────────
# per-project 配置
# ─────────────────────────────────────────────────────────────────────────────

def test_filter_findings_by_config_min_severity():
    """min_severity=HIGH 应过滤掉 LOW 和 MEDIUM 的 finding。"""
    from src.project_config import filter_findings_by_config

    findings = [
        {"file": "f.py", "line_start": 1, "severity": "CRITICAL", "description": "a"},
        {"file": "f.py", "line_start": 2, "severity": "HIGH",     "description": "b"},
        {"file": "f.py", "line_start": 3, "severity": "MEDIUM",   "description": "c"},
        {"file": "f.py", "line_start": 4, "severity": "LOW",      "description": "d"},
    ]
    with patch("src.project_config.load_project_config", return_value={
        "min_severity": "HIGH", "max_findings": 0
    }):
        result = filter_findings_by_config(findings, "owner/repo")

    assert len(result) == 2
    assert all(f["severity"] in ("CRITICAL", "HIGH") for f in result)


def test_filter_findings_by_config_max_findings():
    """max_findings=2 应只保留前 2 条（假设已按 severity 排好序）。"""
    from src.project_config import filter_findings_by_config

    findings = [
        {"file": "f.py", "line_start": i, "severity": "HIGH", "description": f"issue {i}"}
        for i in range(5)
    ]
    with patch("src.project_config.load_project_config", return_value={
        "min_severity": "LOW", "max_findings": 2
    }):
        result = filter_findings_by_config(findings, "owner/repo")

    assert len(result) == 2


def test_filter_findings_zero_max_keeps_all():
    """max_findings=0 表示不限制，保留所有过 min_severity 的 finding。"""
    from src.project_config import filter_findings_by_config

    findings = [
        {"severity": "LOW", "description": f"issue {i}", "file": "f.py", "line_start": i}
        for i in range(10)
    ]
    with patch("src.project_config.load_project_config", return_value={
        "min_severity": "LOW", "max_findings": 0
    }):
        result = filter_findings_by_config(findings, "owner/repo")

    assert len(result) == 10


def test_get_enabled_agents_returns_empty_by_default():
    """空配置（agents=[]）应返回空列表，表示使用规则引擎。"""
    from src.project_config import get_enabled_agents

    with patch("src.project_config.load_project_config", return_value={"agents": []}):
        result = get_enabled_agents("owner/repo")

    assert result == []


def test_get_enabled_agents_returns_whitelist():
    """配置了 agents 列表时，应返回该列表。"""
    from src.project_config import get_enabled_agents

    with patch("src.project_config.load_project_config", return_value={
        "agents": ["SecurityAgent", "QualityAgent"]
    }):
        result = get_enabled_agents("owner/repo")

    assert set(result) == {"SecurityAgent", "QualityAgent"}


def test_project_id_to_filename():
    """owner/repo 应转换为 owner__repo.yaml。"""
    from src.project_config import _project_id_to_filename

    assert _project_id_to_filename("chensiyu47/MindIE-SD_1344") == "chensiyu47__MindIE-SD_1344.yaml"
    assert _project_id_to_filename("org/project") == "org__project.yaml"
