"""SQLAlchemy async 持久化层（aiomysql 驱动）。

职责：
- review_tasks：任务状态机（running → completed / failed），支持断点续跑
- review_results：Agent 级输出（findings / tokens / duration），Checkpoint 核心
- suggestion_status：Suggestion 应用追踪（finding_id 外键）
"""

import json
import logging
import uuid
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from src.config import settings

logger = logging.getLogger(__name__)

# ── 引擎（懒初始化，避免导入时连接）──────────────────────────────────────────
_engine = None
_SessionLocal = None
_tables_ready = False  # 只建表一次


def _get_engine():
    global _engine, _SessionLocal
    if _engine is not None:
        return _engine
    url = settings.MYSQL_URL
    if url.startswith("mysql+pymysql://"):
        url = url.replace("mysql+pymysql://", "mysql+aiomysql://", 1)
    elif url.startswith("mysql://"):
        url = url.replace("mysql://", "mysql+aiomysql://", 1)
    _engine = create_async_engine(url, pool_size=5, max_overflow=10, echo=False)
    _SessionLocal = sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)
    return _engine


async def _ensure_tables() -> None:
    """首次调用时自动建表（幂等），后续调用直接返回。"""
    global _tables_ready
    if _tables_ready:
        return
    await init_tables()
    _tables_ready = True


# ── DDL ───────────────────────────────────────────────────────────────────────

_CREATE_REVIEW_TASKS = """
CREATE TABLE IF NOT EXISTS review_tasks (
    task_id      VARCHAR(64)  NOT NULL PRIMARY KEY,
    project_id   VARCHAR(255) NOT NULL,
    mr_iid       INT          NOT NULL,
    commit_sha   VARCHAR(64)  NOT NULL,
    tier         VARCHAR(20),
    languages    JSON,
    total_files  INT          DEFAULT 0,
    status       VARCHAR(20)  NOT NULL DEFAULT 'running',
    error        TEXT,
    created_at   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_pmr (project_id, mr_iid, commit_sha)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

_CREATE_REVIEW_RESULTS = """
CREATE TABLE IF NOT EXISTS review_results (
    id           INT          NOT NULL AUTO_INCREMENT PRIMARY KEY,
    task_id      VARCHAR(64)  NOT NULL,
    agent_type   VARCHAR(50)  NOT NULL,
    findings_json JSON,
    tokens_in    INT          DEFAULT 0,
    tokens_out   INT          DEFAULT 0,
    duration_ms  INT          DEFAULT 0,
    status       VARCHAR(20)  NOT NULL DEFAULT 'completed',
    created_at   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    INDEX        idx_task (task_id),
    UNIQUE KEY   uq_task_agent (task_id, agent_type)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

_CREATE_SUGGESTION_STATUS = """
CREATE TABLE IF NOT EXISTS suggestion_status (
    id           INT          NOT NULL AUTO_INCREMENT PRIMARY KEY,
    task_id      VARCHAR(64)  NOT NULL,
    finding_id   VARCHAR(64)  NOT NULL,
    project_id   VARCHAR(255),
    mr_iid       INT,
    comment_id   BIGINT,
    file_path    VARCHAR(500),
    line_start   INT,
    severity     VARCHAR(20)  DEFAULT 'LOW',
    status       VARCHAR(20)  NOT NULL DEFAULT 'pending',
    applied_at   DATETIME,
    UNIQUE KEY   uq_finding (finding_id),
    INDEX        idx_task (task_id),
    INDEX        idx_project_mr (project_id, mr_iid)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

# 旧版本升级：为已有表补充缺失列（幂等）
# 注意：MySQL 8.0 不支持 ADD COLUMN IF NOT EXISTS，需在 init_tables 中捕获 1060 错误
_ALTER_SUGGESTION_STATUS = [
    "ALTER TABLE suggestion_status ADD COLUMN project_id VARCHAR(255)",
    "ALTER TABLE suggestion_status ADD COLUMN mr_iid INT",
    "ALTER TABLE suggestion_status ADD COLUMN comment_id BIGINT",
    "ALTER TABLE suggestion_status ADD COLUMN file_path VARCHAR(500)",
    "ALTER TABLE suggestion_status ADD COLUMN line_start INT",
    "ALTER TABLE suggestion_status ADD COLUMN severity VARCHAR(20) DEFAULT 'LOW'",
    "ALTER TABLE suggestion_status ADD INDEX idx_project_mr (project_id, mr_iid)",
]


async def init_tables() -> None:
    """建表（幂等）：服务启动时调用一次。"""
    engine = _get_engine()
    async with engine.begin() as conn:
        for ddl in (_CREATE_REVIEW_TASKS, _CREATE_REVIEW_RESULTS, _CREATE_SUGGESTION_STATUS):
            await conn.execute(text(ddl))
        for alter in _ALTER_SUGGESTION_STATUS:
            try:
                await conn.execute(text(alter))
            except Exception as e:
                msg = str(e)
                # 1060 = Duplicate column name（列已存在）
                # 1061 = Duplicate key name（索引已存在）
                if "Duplicate column" in msg or "Duplicate key" in msg:
                    pass
                else:
                    logger.warning("ALTER TABLE skipped: %s", msg)
    logger.info("DB tables initialized (or already exist)")


# ── CRUD ──────────────────────────────────────────────────────────────────────

async def create_or_get_task(  # noqa: PLR0913
    project_id: str,
    mr_iid: int,
    commit_sha: str,
    tier: str = "",
    languages: list[str] | None = None,
    total_files: int = 0,
) -> str:
    """创建检视任务，若同 commit 任务已存在则返回已有 task_id（幂等）。

    同一 commit 的任务可能因 Webhook 重发而重复触发，通过 (project_id, mr_iid, commit_sha)
    联合查询保证幂等。首次调用时自动建表。
    """
    await _ensure_tables()
    _get_engine()
    assert _SessionLocal is not None

    async with _SessionLocal() as session:
        result = await session.execute(
            text(
                "SELECT task_id FROM review_tasks "
                "WHERE project_id=:pid AND mr_iid=:mr AND commit_sha=:sha "
                "LIMIT 1"
            ),
            {"pid": project_id, "mr": mr_iid, "sha": commit_sha},
        )
        row = result.fetchone()
        if row:
            return str(row[0])

        task_id = uuid.uuid4().hex
        await session.execute(
            text(
                "INSERT INTO review_tasks "
                "(task_id, project_id, mr_iid, commit_sha, tier, languages, total_files, status) "
                "VALUES (:tid, :pid, :mr, :sha, :tier, :langs, :files, 'running')"
            ),
            {
                "tid":   task_id,
                "pid":   project_id,
                "mr":    mr_iid,
                "sha":   commit_sha,
                "tier":  tier,
                "langs": json.dumps(languages or [], ensure_ascii=False),
                "files": total_files,
            },
        )
        await session.commit()
        return task_id


async def load_agent_findings(task_id: str) -> dict[str, list[dict]]:
    """加载已完成 Agent 的 findings（Checkpoint 恢复，断点续跑）。

    返回 {agent_type: findings_list}，只包含 status='completed' 的条目。
    """
    _get_engine()
    assert _SessionLocal is not None

    async with _SessionLocal() as session:
        result = await session.execute(
            text(
                "SELECT agent_type, findings_json FROM review_results "
                "WHERE task_id=:tid AND status='completed'"
            ),
            {"tid": task_id},
        )
        rows = result.fetchall()

    out: dict[str, list[dict]] = {}
    for agent_type, findings_json in rows:
        try:
            findings = json.loads(findings_json) if findings_json else []
        except Exception:
            findings = []
        out[str(agent_type)] = findings
    return out


async def save_agent_result(
    task_id: str,
    agent_type: str,
    findings: list[dict],
    tokens_in: int = 0,
    tokens_out: int = 0,
    duration_ms: int = 0,
    status: str = "completed",
) -> None:
    """Agent 完成后立即持久化（INSERT OR REPLACE 保证幂等，支持重试覆盖失败记录）。"""
    _get_engine()
    assert _SessionLocal is not None

    async with _SessionLocal() as session:
        await session.execute(
            text(
                "INSERT INTO review_results "
                "(task_id, agent_type, findings_json, tokens_in, tokens_out, duration_ms, status) "
                "VALUES (:tid, :agent, :fj, :ti, :to_, :dur, :st) "
                "ON DUPLICATE KEY UPDATE "
                "findings_json=VALUES(findings_json), tokens_in=VALUES(tokens_in), "
                "tokens_out=VALUES(tokens_out), duration_ms=VALUES(duration_ms), status=VALUES(status)"
            ),
            {
                "tid":   task_id,
                "agent": agent_type,
                "fj":    json.dumps(findings, ensure_ascii=False),
                "ti":    tokens_in,
                "to_":   tokens_out,
                "dur":   duration_ms,
                "st":    status,
            },
        )
        await session.commit()


async def complete_task(task_id: str) -> None:
    """将任务标记为 completed。"""
    _get_engine()
    assert _SessionLocal is not None

    async with _SessionLocal() as session:
        await session.execute(
            text("UPDATE review_tasks SET status='completed', updated_at=:now WHERE task_id=:tid"),
            {"now": datetime.now(), "tid": task_id},
        )
        await session.commit()


async def fail_task(task_id: str, error: str = "") -> None:
    """将任务标记为 failed，记录错误信息。"""
    _get_engine()
    assert _SessionLocal is not None

    async with _SessionLocal() as session:
        await session.execute(
            text(
                "UPDATE review_tasks "
                "SET status='failed', error=:err, updated_at=:now "
                "WHERE task_id=:tid"
            ),
            {"err": error[:2000], "now": datetime.now(), "tid": task_id},
        )
        await session.commit()


async def save_suggestion(
    task_id: str,
    finding_id: str,
    project_id: str,
    mr_iid: int,
    comment_id: int,
    file_path: str,
    line_start: int,
    severity: str = "LOW",
) -> None:
    """记录一条已发布的 suggestion（状态=pending）。"""
    _get_engine()
    assert _SessionLocal is not None

    async with _SessionLocal() as session:
        await session.execute(
            text(
                "INSERT IGNORE INTO suggestion_status "
                "(task_id, finding_id, project_id, mr_iid, comment_id, file_path, line_start, severity, status) "
                "VALUES (:tid, :fid, :pid, :mr, :cid, :fp, :ln, :sev, 'pending')"
            ),
            {
                "tid": task_id,
                "fid": finding_id,
                "pid": project_id,
                "mr":  mr_iid,
                "cid": comment_id,
                "fp":  file_path,
                "ln":  line_start,
                "sev": severity,
            },
        )
        await session.commit()


async def mark_suggestions_applied(project_id: str, file_paths: list[str]) -> int:
    """将指定项目中匹配文件路径的 pending suggestions 标记为 applied。

    Push 事件 "Apply suggestion" commit 触发此函数。
    返回更新的行数。
    """
    if not file_paths:
        return 0
    _get_engine()
    assert _SessionLocal is not None

    placeholders = ", ".join(f":fp{i}" for i in range(len(file_paths)))
    params: dict = {"pid": project_id, "now": datetime.now()}
    for i, fp in enumerate(file_paths):
        params[f"fp{i}"] = fp

    async with _SessionLocal() as session:
        result = await session.execute(
            text(
                f"UPDATE suggestion_status "
                f"SET status='applied', applied_at=:now "
                f"WHERE project_id=:pid AND status='pending' "
                f"AND file_path IN ({placeholders})"
            ),
            params,
        )
        await session.commit()
        return result.rowcount


async def get_open_mr_ids(project_id: str, file_paths: list[str]) -> list[int]:
    """获取指定文件路径下存在 pending suggestion 的所有 mr_iid（去重）。

    在 mark_suggestions_applied 之前调用，用于确定哪些 MR 需要重算风险标签。
    """
    if not file_paths:
        return []
    _get_engine()
    assert _SessionLocal is not None

    placeholders = ", ".join(f":fp{i}" for i in range(len(file_paths)))
    params: dict = {"pid": project_id}
    for i, fp in enumerate(file_paths):
        params[f"fp{i}"] = fp

    async with _SessionLocal() as session:
        result = await session.execute(
            text(
                f"SELECT DISTINCT mr_iid FROM suggestion_status "
                f"WHERE project_id=:pid AND status='pending' "
                f"AND file_path IN ({placeholders})"
            ),
            params,
        )
        rows = result.fetchall()
    return [int(row[0]) for row in rows if row[0] is not None]


async def count_open_critical_high(project_id: str, mr_iid: int) -> int:
    """统计指定 MR 中仍为 pending 状态的 CRITICAL/HIGH severity suggestion 数量。

    suggestion apply 后触发，用于重算 ai-risk 标签。
    """
    _get_engine()
    assert _SessionLocal is not None

    async with _SessionLocal() as session:
        result = await session.execute(
            text(
                "SELECT COUNT(*) FROM suggestion_status "
                "WHERE project_id=:pid AND mr_iid=:mr "
                "AND status='pending' AND severity IN ('CRITICAL', 'HIGH')"
            ),
            {"pid": project_id, "mr": mr_iid},
        )
        row = result.fetchone()
    return int(row[0]) if row else 0
