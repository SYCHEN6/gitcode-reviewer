"""Redis 分布式并发控制（per-MR 锁 + 全局信号量）。

支持多进程 / 多实例横向扩展：
  1. per-MR 分布式锁：key=review:lock:{project}:{mr_iid}，SET NX + Lua 安全释放
     保证同一 MR 的多次 push/force-push 在任意实例上都串行执行
  2. 全局分布式信号量：key=review:semaphore:active，Lua 原子 INCR+检查
     限制所有实例合计并发数 ≤ MAX_CONCURRENT_REVIEWS
"""

import asyncio
import logging
import uuid
from contextlib import asynccontextmanager

import redis.asyncio as aioredis

from src.config import settings

logger = logging.getLogger(__name__)

# ── Redis 连接（复用，懒初始化）──────────────────────────────────────────────
_redis_client: aioredis.Redis | None = None


def _get_redis() -> aioredis.Redis:
    global _redis_client
    if _redis_client is None:
        _redis_client = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
    return _redis_client


# ── 常量 ──────────────────────────────────────────────────────────────────────
_LOCK_TTL_SECONDS    = 3600   # MR 锁的安全 TTL（兜底：进程崩溃也不会永久锁死）
_SEMAPHORE_REDIS_KEY = "review:semaphore:active"
_SEMAPHORE_TTL       = 3600   # 信号量计数器的 TTL 安全保障

# Lua：原子地 INCR + 检查上限；超出则撤回，返回 0
_LUA_SEMAPHORE_ACQUIRE = """
local c = redis.call('INCR', KEYS[1])
redis.call('EXPIRE', KEYS[1], ARGV[2])
if c > tonumber(ARGV[1]) then
    redis.call('DECR', KEYS[1])
    return 0
end
return c
"""

# Lua：只有当 key 的值等于 owner 时才删除（防止误删其他进程的锁）
_LUA_LOCK_RELEASE = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
else
    return 0
end
"""


@asynccontextmanager
async def _distributed_mr_lock(project_id: str, mr_iid: int, timeout_seconds: int = 120):
    """分布式 MR 锁（跨进程/实例安全）。

    同一 project+mr_iid 同一时刻只允许一个检视任务运行；
    如果有其他实例正在检视同一 MR，等待直到对方完成或超时。
    超时后不阻断，只记录 warning 继续执行（降级为尽力而为）。
    """
    redis = _get_redis()
    lock_key = f"review:lock:{project_id}:{mr_iid}"
    owner = uuid.uuid4().hex
    acquired = False

    deadline = asyncio.get_event_loop().time() + timeout_seconds
    while asyncio.get_event_loop().time() < deadline:
        ok = await redis.set(lock_key, owner, nx=True, ex=_LOCK_TTL_SECONDS)
        if ok:
            acquired = True
            break
        await asyncio.sleep(2)

    if not acquired:
        logger.warning(
            "MR lock timeout for %s#%d (waited %ds), proceeding anyway",
            project_id, mr_iid, timeout_seconds,
        )

    try:
        yield
    finally:
        if acquired:
            try:
                await redis.eval(_LUA_LOCK_RELEASE, 1, lock_key, owner)
            except Exception as ex:
                logger.warning("Failed to release MR lock: %s", ex)


@asynccontextmanager
async def _distributed_global_semaphore(max_count: int, timeout_seconds: int = 60):
    """分布式全局信号量（跨进程/实例安全）。

    使用 Redis counter + Lua 原子脚本确保 INCR+检查的原子性，
    避免 TOCTOU 竞态。超时后不阻断，降级为尽力而为。
    """
    redis = _get_redis()
    acquired = False

    deadline = asyncio.get_event_loop().time() + timeout_seconds
    while asyncio.get_event_loop().time() < deadline:
        slot = await redis.eval(
            _LUA_SEMAPHORE_ACQUIRE, 1, _SEMAPHORE_REDIS_KEY, max_count, _SEMAPHORE_TTL
        )
        if slot:
            acquired = True
            break
        await asyncio.sleep(2)

    if not acquired:
        logger.warning(
            "Global review semaphore full (max=%d, waited %ds), proceeding anyway",
            max_count, timeout_seconds,
        )

    try:
        yield
    finally:
        if acquired:
            try:
                await redis.decr(_SEMAPHORE_REDIS_KEY)
            except Exception as ex:
                logger.warning("Failed to release global semaphore slot: %s", ex)


async def write_review_metrics(
    project_id: str,
    mr_iid: int,
    commit_sha: str,
    pr_stats: dict,
    languages: list[str],
    final_state: dict,
    total_ms: int,
) -> None:
    """将本次检视的结构化 metrics 写入 Redis（TTL 7天，供监控大屏读取）。

    key 格式：review:metrics:{project_id}:{mr_iid}:{sha8}
    """
    import json
    from datetime import datetime

    review_id = f"{project_id}:{mr_iid}:{commit_sha[:8]}"

    # per-agent findings 统计（从聚合 findings 归类）
    agent_stats: dict[str, dict] = {}
    for f in final_state.get("findings", []):
        agent = f.get("agent", "unknown")
        bucket = agent_stats.setdefault(agent, {"findings_raw": 0})
        bucket["findings_raw"] += 1

    raw_count   = len(final_state.get("findings", []))
    final_count = len(final_state.get("final_findings", []))

    metrics = {
        "review_id": review_id,
        "tier":      pr_stats.get("tier", "unknown"),
        "languages": languages,
        "agents":    agent_stats,
        "synthesize": {"in": raw_count, "out": final_count},
        "total_ms":  total_ms,
        "timestamp": datetime.now().isoformat(),
    }
    try:
        redis = _get_redis()
        await redis.setex(
            f"review:metrics:{review_id}",
            86400 * 7,
            json.dumps(metrics, ensure_ascii=False),
        )
        logger.info(
            "review metrics written: %s raw=%d final=%d total_ms=%d",
            review_id, raw_count, final_count, total_ms,
        )
    except Exception as e:
        logger.warning("Failed to write review metrics to Redis: %s", e)
