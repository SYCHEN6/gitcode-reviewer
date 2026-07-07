"""LangGraph ReviewOrchestrator — Multi-Agent PR 自动检视主图。

图结构：
  supervisor_node
    ↓ DISPATCH → run_agents_node → supervisor_node（循环，最多 5 轮）
    ↓ FINISH   → summary_node → synthesize_node → critic_node → publish_node → END
"""

import asyncio
import json
import logging
import operator
import os
import re
import uuid
from collections import Counter
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated, TypedDict

import redis.asyncio as aioredis
from langgraph.graph import END, StateGraph

from src.agents.performance_agent import run_performance_agent
from src.agents.quality_agent import run_quality_agent
from src.agents.logic_agent import run_logic_agent
from src.agents.security_agent import run_security_agent
from src.agents.summary_agent import run_summary_agent
from src.agents.supervisor import get_focus_hints, run_supervisor
from src.config import settings
from src.tools.gitcode_client import GitCodeClient

logger = logging.getLogger(__name__)

# ── Redis 连接（复用，懒初始化）──────────────────────────────────────────────
_redis_client: aioredis.Redis | None = None


def _get_redis() -> aioredis.Redis:
    global _redis_client
    if _redis_client is None:
        _redis_client = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
    return _redis_client


# ── 并发控制（Redis 分布式，支持多进程 / 多实例部署）────────────────────────
# 分布式设计说明：
#   1. per-MR 分布式锁：key=review:lock:{project}:{mr_iid}，SET NX + Lua 安全释放
#      保证同一 MR 的多次 push/force-push 在任意实例上都串行执行，不会并发刷评论
#   2. 全局分布式信号量：key=review:semaphore:active，Lua 原子 INCR+检查
#      限制所有实例合计并发数 ≤ MAX_CONCURRENT_REVIEWS，防止 LLM API 过载

_LOCK_TTL_SECONDS    = 3600   # MR 锁的安全 TTL（兜底：如果进程崩溃也不会永久锁死）
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
            "Distributed MR lock timeout for %s#%d (waited %ds), proceeding without lock",
            project_id, mr_iid, timeout_seconds,
        )

    try:
        yield
    finally:
        if acquired:
            try:
                await redis.eval(_LUA_LOCK_RELEASE, 1, lock_key, owner)
            except Exception as ex:
                logger.warning("Failed to release MR lock %s: %s", lock_key, ex)


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


_AGENT_MAP = {
    "SecurityAgent":    run_security_agent,
    "LogicAgent":       run_logic_agent,
    "QualityAgent":     run_quality_agent,
    "PerformanceAgent": run_performance_agent,
}

_RISK_LABEL = {
    "CRITICAL": "ai-risk-high",
    "HIGH":     "ai-risk-high",
    "MEDIUM":   "ai-risk-low",
    "LOW":      "ai-risk-low",
}

# 自动创建标签时使用的颜色（Gitee/GitCode API color 不带 # 前缀）
_LABEL_COLOR = {
    "ai-risk-high": "e11d48",  # 红色
    "ai-risk-low":  "f59e0b",  # 橙黄色
}


# ── State ──────────────────────────────────────────────────────────────────

class ReviewState(TypedDict):
    # 输入
    project_id: str
    mr_iid:     int
    commit_sha: str
    task_id:    str    # DB 任务 ID（空字符串表示 DB 不可用，跳过持久化）

    # init 阶段填充
    raw_diff:   str
    file_list:  list[str]
    diffs:      list[dict]
    head_sha:   str
    base_sha:   str
    pr_stats:   dict   # {files, lines_added, lines_removed, tier}
    languages:  list[str]  # 从文件扩展名检测出的编程语言（如 ["Python", "Go"]）

    # Supervisor 循环控制
    iteration:            int
    supervisor_action:    str
    supervisor_reasoning: Annotated[list[str], operator.add]
    pr_meta:              dict
    agents_to_dispatch:   list[dict]

    # 专家 Agent 聚合输出
    findings: Annotated[list[dict], operator.add]

    # 最终输出
    summary:        dict
    final_findings: list[dict]


# ── 工具函数 ───────────────────────────────────────────────────────────────

# ── 不可检视文件过滤 ────────────────────────────────────────────────────────
# 扩展名列表对齐 PR-Agent language_extensions.toml（battle-tested 业界标准），
# 以扩展名为唯一过滤维度，不做二进制内容检测。
# 注意：DB migrations（.sql 变更）明确不在此列表，需要检视。

_SKIP_EXTENSIONS = frozenset({
    # 图片 / 图标
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".svg", ".ico",
    ".tiff", ".tif", ".raw", ".psd",
    # 音视频
    ".mp3", ".mp4", ".wav", ".ogg", ".webm", ".avi", ".mov", ".mkv", ".flac",
    # 文档 / 表格（二进制格式，非纯文本）
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    # 压缩包 / 归档
    ".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".zst", ".rar", ".7z",
    # 原生二进制 / 编译产物
    ".exe", ".dll", ".so", ".dylib", ".a", ".o", ".obj", ".bin",
    ".pyc", ".pyd", ".pyo", ".class", ".whl", ".jar", ".war", ".egg",
    # 字体
    ".ttf", ".woff", ".woff2", ".eot", ".otf",
    # 锁文件（通用 .lock 扩展名）
    ".lock", ".lockb", ".snap",
    # 日志（通常是运行时生成，无需检视）
    ".log",
    # 数据 / ML 产物
    ".csv", ".tsv", ".dat",
    ".pkl", ".pickle", ".npy", ".npz",
    ".pt", ".pth", ".ckpt", ".safetensors", ".onnx", ".pb", ".tflite",
    ".h5", ".hdf5", ".parquet",
    # 数据库文件
    ".db", ".sqlite", ".sqlite3",
})

# 精确文件名匹配（basename），覆盖无扩展名或特殊命名的锁文件
_SKIP_BASENAMES = frozenset({
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
    "Pipfile.lock", "poetry.lock",
    "Cargo.lock", "composer.lock", "go.sum",
    "Gemfile.lock", "pubspec.lock",
})

# 路径前缀：生成物 / 第三方依赖目录
_SKIP_PATH_SEGMENTS = (
    "node_modules/", "dist/", "build/", "__pycache__/",
    ".git/", "vendor/", "third_party/", "site-packages/",
)

# 后缀：压缩/生成的 JS/CSS，以及 protobuf 生成的 Go 文件
_SKIP_NAME_SUFFIXES = (".min.js", ".min.css", ".bundle.js", ".map", ".pb.go")

# ── Token 预算（对齐 PR-Agent 的 soft/hard threshold 策略）────────────────
# Claude Sonnet 200K context；预留 8K 给 system prompt + human wrapper，4K 给输出
_TOKEN_BUDGET = 188_000        # tokens 可用于 diff
_CHARS_PER_TOKEN = 4           # 保守估算（代码比英文散文更密，实际约 3-4）
_SOFT_THRESHOLD_CHARS = int(_TOKEN_BUDGET * _CHARS_PER_TOKEN * 0.85)  # ~640K，触发警告
_HARD_THRESHOLD_CHARS = int(_TOKEN_BUDGET * _CHARS_PER_TOKEN)          # ~752K，停止添加文件


def _is_reviewable(filename: str, patch: str) -> tuple[bool, str]:
    """判断文件是否可检视，返回 (可检视, 跳过原因)。"""
    if not patch:
        return False, "no patch (binary or rename-only)"

    lower = filename.lower()
    _, ext = os.path.splitext(lower)
    basename = os.path.basename(lower)

    if ext in _SKIP_EXTENSIONS:
        return False, f"extension {ext}"
    if basename in _SKIP_BASENAMES:
        return False, f"lock/generated filename"
    if any(lower.startswith(seg) or f"/{seg}" in lower for seg in _SKIP_PATH_SEGMENTS):
        return False, "generated/vendor path"
    if any(lower.endswith(sfx) for sfx in _SKIP_NAME_SUFFIXES):
        return False, "minified/generated suffix"

    return True, ""


def _filter_reviewable_diffs(diffs: list[dict]) -> tuple[list[dict], list[dict]]:
    """两步过滤，对齐 PR-Agent soft/hard threshold 策略：

    1. 扩展名/路径过滤：去除二进制、lock、生成物等不可检视文件
    2. Token 预算过滤：顺序累加 diff 大小；
       超过 soft threshold 时记录警告；超过 hard threshold 时停止添加（文件级跳过，不截断）

    返回 (可检视列表, 跳过列表)，跳过列表含跳过原因。
    """
    reviewable: list[dict] = []
    skipped: list[dict] = []
    total_chars = 0

    for d in diffs:
        fname = d.get("filename", "")
        patch = d.get("patch", "")
        if isinstance(patch, dict):
            patch = patch.get("diff", "")

        # 第一步：类型过滤
        ok, reason = _is_reviewable(fname, patch)
        if not ok:
            skipped.append({"file": fname, "reason": reason})
            continue

        # 第二步：token 预算（hard threshold 在文件边界停止，不截断文件内部）
        patch_chars = len(patch)
        if total_chars + patch_chars > _HARD_THRESHOLD_CHARS:
            skipped.append({"file": fname, "reason": "exceeded token hard threshold"})
            logger.warning("token hard threshold reached, skipping %s (%d chars)", fname, patch_chars)
            continue

        if total_chars + patch_chars > _SOFT_THRESHOLD_CHARS:
            logger.warning("token soft threshold exceeded at %s, total=%d chars", fname, total_chars)

        reviewable.append(d)
        total_chars += patch_chars

    return reviewable, skipped


def _calc_pr_stats(diffs: list[dict]) -> dict:
    """计算 PR 规模指标，用于 Supervisor 分级决策。"""
    lines_added = lines_removed = 0
    for d in diffs:
        patch = d.get("patch", "")
        if isinstance(patch, dict):
            patch = patch.get("diff", "")
        for line in (patch or "").splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                lines_added += 1
            elif line.startswith("-") and not line.startswith("---"):
                lines_removed += 1

    files = len(diffs)
    total_lines = lines_added + lines_removed

    # 阈值依据：SmartBear/Cisco 研究 + PropelCode 50,000+ PR 数据集
    # small  ≤50行 / ≤3文件：单次 hotfix / 配置调整，87% 缺陷检出率
    # medium ≤500行 / ≤15文件：正常功能迭代，研究推荐的 75th percentile 上限
    # large  ≤1000行 / ≤30文件：大功能，检出率降至 42-65%，需拆批并行
    # xl     >1000行 或 >30文件：检出率仅 28%，应提示拆分 PR
    if total_lines <= 50 and files <= 3:
        tier = "small"
    elif total_lines <= 500 and files <= 15:
        tier = "medium"
    elif total_lines <= 1000 and files <= 30:
        tier = "large"
    else:
        tier = "xl"

    return {
        "files":         files,
        "lines_added":   lines_added,
        "lines_removed": lines_removed,
        "tier":          tier,
    }


# ── 编程语言检测 ──────────────────────────────────────────────────────────────
# 扩展名 → 语言名（与 PR-Agent language_extensions.toml 对齐，覆盖主流语言）
_EXT_TO_LANG: dict[str, str] = {
    # Python 生态
    ".py": "Python", ".pyi": "Python", ".pyx": "Python",
    # JVM
    ".java": "Java", ".kt": "Kotlin", ".kts": "Kotlin", ".scala": "Scala", ".groovy": "Groovy",
    # Go
    ".go": "Go",
    # Web 前端
    ".js": "JavaScript", ".mjs": "JavaScript", ".cjs": "JavaScript",
    ".jsx": "JavaScript",
    ".ts": "TypeScript", ".tsx": "TypeScript",
    ".vue": "Vue", ".svelte": "Svelte",
    ".html": "HTML", ".htm": "HTML",
    ".css": "CSS", ".scss": "CSS", ".less": "CSS",
    # 系统语言
    ".c": "C", ".h": "C/C++",
    ".cpp": "C++", ".cc": "C++", ".cxx": "C++", ".hpp": "C++", ".hxx": "C++",
    ".rs": "Rust",
    ".swift": "Swift",
    ".m": "Objective-C",
    # 脚本
    ".rb": "Ruby",
    ".php": "PHP",
    ".sh": "Shell", ".bash": "Shell", ".zsh": "Shell", ".fish": "Shell",
    ".lua": "Lua",
    ".r": "R",
    ".dart": "Dart",
    ".cs": "C#", ".fs": "F#",
    # 数据 / 配置（仍需检视逻辑）
    ".sql": "SQL",
    ".proto": "Protobuf",
    ".tf": "Terraform", ".hcl": "Terraform",
    ".yaml": "YAML", ".yml": "YAML",
    ".toml": "TOML",
    ".json": "JSON",
    ".xml": "XML",
}


def _detect_languages(diffs: list[dict]) -> list[str]:
    """从变更文件扩展名识别本次 PR 涉及的编程语言，返回去重后的有序列表。"""
    langs: set[str] = set()
    for d in diffs:
        _, ext = os.path.splitext(d.get("filename", "").lower())
        lang = _EXT_TO_LANG.get(ext)
        if lang:
            langs.add(lang)
    return sorted(langs)


def _detect_new_files(diffs: list[dict], reviewable_set: set[str]) -> list[str]:
    """从 diffs 中识别新增文件（status=added 或 patch 以 -0,0 开头）。

    新增文件需要感知目录结构，以便 Agent 判断命名冲突、重复实现等问题。
    """
    new_files: list[str] = []
    for d in diffs:
        fname = d.get("filename", "")
        if fname not in reviewable_set:
            continue
        # 优先用 status 字段（GitHub/Gitee 标准），兜底检查 patch 首个 hunk
        status = d.get("status", "")
        if status == "added":
            new_files.append(fname)
            continue
        patch = d.get("patch", "")
        if isinstance(patch, dict):
            patch = patch.get("diff", "")
        # "new file mode" 出现在 diff 头部，或首个 hunk 是 @@ -0,0 + 表示从无到有
        if patch and re.search(r"^@@\s+-0,0\s+\+", patch, re.MULTILINE):
            new_files.append(fname)
    return new_files


# ── 首轮规则引擎派遣 ──────────────────────────────────────────────────────────
# 路径关键词来源：SmartBear 研究 + PR-Agent 实践（按语义分组）
_ML_KEYWORDS = frozenset({
    "layer", "layers", "model", "models", "attn", "attention",
    "flash_attn", "train", "trainer", "training", "inference",
    "module", "backbone", "encoder", "decoder", "embedding",
})
_SECURITY_KEYWORDS = frozenset({
    "auth", "authentication", "authz", "authorization",
    "crypto", "encrypt", "decrypt", "hash", "hmac", "sign",
    "login", "logout", "session", "password", "passwd",
    "token", "jwt", "oauth", "apikey", "secret",
    "permission", "acl", "rbac", "policy",
})
_SECURITY_EXTENSIONS = frozenset({".sql"})


def _rule_engine_dispatch(
    files: list[str],
    languages: list[str],
    tier: str,
    pr_stats: dict,
) -> list[dict]:
    """首轮确定性 Agent 派遣（不依赖 LLM，0 延迟）。

    规则优先级（由高到低）：
    1. medium/large/xl → 全派（研究显示小 PR 需要聚焦，中大型 PR 值得全量检查）
    2. 路径含 ML 关键词 → 加 PerformanceAgent
    3. 路径含安全关键词 / .sql 扩展名 → 加 SecurityAgent
    4. 其余 → 只派 QualityAgent + LogicAgent（small PR 最小成本覆盖）

    focus_hint 由 LLM Advisor（get_focus_hints）在调用方注入，此处留空。
    """
    needs_security = tier in ("medium", "large", "xl")
    needs_performance = tier in ("medium", "large", "xl")

    for fpath in files:
        lower = fpath.lower()
        _, ext = os.path.splitext(lower)
        # 把路径拆分为 token（按 / _ - . 分割）
        tokens = set(re.split(r"[/_\-.]", lower))

        if tokens & _ML_KEYWORDS:
            needs_performance = True
        if tokens & _SECURITY_KEYWORDS or ext in _SECURITY_EXTENSIONS:
            needs_security = True

        if needs_security and needs_performance:
            break  # 已全派，可以提前退出

    agents: list[str] = ["QualityAgent", "LogicAgent"]
    if needs_security:
        agents.insert(0, "SecurityAgent")
    if needs_performance:
        agents.append("PerformanceAgent")

    # 保留顺序并去重（SecurityAgent/LogicAgent/QualityAgent/PerformanceAgent）
    seen: set[str] = set()
    ordered = []
    for a in ["SecurityAgent", "LogicAgent", "QualityAgent", "PerformanceAgent"]:
        if a in agents and a not in seen:
            seen.add(a)
            ordered.append(a)

    return [{"agent_type": t, "files": files, "focus_hint": ""} for t in ordered]


def _extract_diff_slice(diffs: list[dict], files: list[str]) -> str:
    """提取指定文件集合的完整 diff，按文件粒度拼接，不做字符截断。

    不可检视文件（二进制/图片/lock 等）已在入口处过滤，
    可检视的代码文件始终传入完整 diff，不在 hunk 中间截断。
    """
    file_set = set(files)
    parts = []
    for d in diffs:
        fname = d.get("filename", "")
        if fname not in file_set:
            continue
        patch = d.get("patch", "")
        if isinstance(patch, dict):
            patch = patch.get("diff", "")
        if not patch:
            continue
        prev = d.get("previous_filename") or fname
        parts.append(f"--- a/{prev}\n+++ b/{fname}\n{patch}")
    return "\n".join(parts)


def _nearest_added_line(patch_text: str, target_line: int) -> int | None:
    """若 target_line 本身是 diff 中的 + 行，返回该行号；否则返回 None。

    要求精确匹配：context 行、- 行、或不在 diff 中的行一律返回 None，
    表示该 finding 不对应本次 PR 修改，不应发 inline comment。
    """
    entries: list[tuple[int, bool]] = []  # (file_line, is_added)
    new_line = 0
    in_hunk = False

    for raw_line in patch_text.splitlines():
        if raw_line.startswith("@@"):
            in_hunk = True
            m = re.search(r"\+(\d+)", raw_line)
            if m:
                new_line = int(m.group(1)) - 1
            continue
        if not in_hunk:
            continue
        if raw_line.startswith("-"):
            continue
        new_line += 1
        entries.append((new_line, raw_line.startswith("+")))

    # 只接受精确匹配的 + 行（要求 Agent 报告的行号就是 diff 中的 + 行）
    # 不允许 snapping 到邻近行，避免把 context 行问题贴到相邻 + 行上
    for ln, added in entries:
        if ln == target_line and added:
            return ln
    return None  # 该行不是 + 行 → 未被本次 PR 修改，不发 inline comment


def _compute_diff_position(patch_text: str, target_line: int) -> int | None:
    """兼容旧调用：返回 patch 里 target_line 附近 + 行是否存在（非 None 表示在 diff 中）。"""
    return _nearest_added_line(patch_text, target_line)


def _get_range_text(patch_text: str, line_start: int, line_end: int) -> str:
    """返回 patch 里 [line_start, line_end] 区间内所有行的拼接文本（不含 +/空格前缀）。

    finding 可能跨多行（如 PerformanceAgent 把 line 239 + 240 合并报告），
    需要检查整个区间内是否有 description 声称的关键词，而不只看 line_start 那一行。
    """
    new_line = 0
    in_hunk = False
    collected: list[str] = []
    for raw_line in patch_text.splitlines():
        if raw_line.startswith("@@"):
            in_hunk = True
            m = re.search(r"\+(\d+)", raw_line)
            if m:
                new_line = int(m.group(1)) - 1
            continue
        if not in_hunk or raw_line.startswith("-"):
            continue
        new_line += 1
        if line_start <= new_line <= line_end:
            collected.append(raw_line[1:] if raw_line else "")
        elif new_line > line_end:
            break
    return "\n".join(collected)


# (description 正则, 代码中必须出现的字符串)
# description 匹配用 \b 词边界，避免 "fingerprint" 触发 "print" 规则
_DESC_CODE_CHECKS: list[tuple[str, str]] = [
    (r"\bprint\b",    "print("),
    (r"console\.log", "console.log"),
    (r"\bdebugger\b", "debugger"),
    (r"\bpprint\b",   "pprint("),
]


def _description_plausible(description: str, code_text: str) -> bool:
    """检查 description 中声称的关键词是否出现在实际代码区间内。

    只做"反证"：description 说有 X 但代码里没有 X → 大概率行号错误，丢弃。
    无法匹配任何规则时默认通过。
    """
    if not code_text:
        return True
    desc_lower = description.lower()
    code_lower = code_text.lower()
    for desc_pattern, code_kw in _DESC_CODE_CHECKS:
        if re.search(desc_pattern, desc_lower) and code_kw not in code_lower:
            return False
    return True


def _patch_text_for_file(diffs: list[dict], filename: str) -> str:
    for d in diffs:
        if d.get("filename") == filename:
            patch = d.get("patch", "")
            if isinstance(patch, dict):
                patch = patch.get("diff", "")
            return patch or ""
    return ""


_AI_SECTION_START = "<!-- AI-REVIEW-START -->"
_AI_SECTION_END   = "<!-- AI-REVIEW-END -->"
_SEV_EMOJI = {"CRITICAL": "🔴", "HIGH": "🟠", "MEDIUM": "🟡", "LOW": "🔵"}
_AI_AGENTS = {"SecurityAgent", "LogicAgent", "QualityAgent", "PerformanceAgent"}


def _severity_order(s: str) -> int:
    return {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}.get(s, 4)


_DESC_STOP = frozenset({
    "的", "在", "中", "是", "不", "和", "有", "为", "这", "以", "但", "或", "且",
    "了", "也", "都", "被", "将", "该", "于", "对", "其", "与", "并", "由", "等",
})


def _desc_keywords(desc: str) -> frozenset[str]:
    """从描述中提取有意义的词（中英文均支持），用于跨 Agent 同行去重。

    - 英文标识符按下划线拆分（bsa_sparse_attention_v3 → bsa/sparse/attention/v3）
    - 短中文序列（≤4 字）保留整体；长序列（>4 字）拆为 2-char bigrams，
      避免整段无空格中文被当成一个"超长 token"而无法参与重叠计算
    """
    raw = re.findall(r"[A-Za-z_]\w*|[一-鿿]{2,}", desc)
    tokens: list[str] = []
    for t in raw:
        if re.match(r"[A-Za-z_]", t):
            if "_" in t:
                tokens.extend(p for p in t.split("_") if len(p) > 1)
            else:
                tokens.append(t)
        else:
            if len(t) <= 4:
                tokens.append(t)
            else:
                tokens.extend(t[i:i + 2] for i in range(len(t) - 1))
    return frozenset(t for t in tokens if len(t) > 1 and t not in _DESC_STOP)


def _desc_overlap(d1: str, d2: str) -> float:
    """计算两条描述的关键词重叠系数（交集 / 较小集合大小）。"""
    k1 = _desc_keywords(d1)
    k2 = _desc_keywords(d2)
    if not k1 or not k2:
        return 0.0
    return len(k1 & k2) / min(len(k1), len(k2))


def _desc_should_merge(d1: str, d2: str) -> bool:
    """判断两条描述是否描述同一问题（用于跨 Agent 同行去重）。

    双层判据：
    1. 关键词重叠系数 >= 0.25（主判据，覆盖大多数情况）
    2. 共享至少 1 个 4+ 字符英文标识符（代码级 token，如函数名、变量名）
       ──用于 overlap 偏低但明显指向同一代码符号的情况，如两条描述均提及 "print"
    """
    k1 = _desc_keywords(d1)
    k2 = _desc_keywords(d2)
    if not k1 or not k2:
        return False
    common = k1 & k2
    if not common:
        return False
    if len(common) / min(len(k1), len(k2)) >= 0.25:
        return True
    code_common = {t for t in common if re.match(r"[A-Za-z]", t) and len(t) >= 4}
    return bool(code_common)


def _find_ai_summary_comment(comments: list[dict]) -> dict | None:
    """从评论列表中找到 AI 总结评论（含 AI-REVIEW-START 标记的那一条）。"""
    for c in comments:
        if _AI_SECTION_START in (c.get("body", "") or ""):
            return c
    return None


# 我们发出的 inline comment 格式："{emoji} **[{SEVERITY}]** `file:line`"
_AI_INLINE_RE = re.compile(r"[🔴🟠🟡🔵] \*\*\[(?:CRITICAL|HIGH|MEDIUM|LOW)\]\*\*")


def _parse_reported_keys(comments: list[dict]) -> set[tuple]:
    """从已有评论中提取已报告发现的三元组 (file, line_start, desc_40)，用于跨轮去重。

    业界标准：同一行不同发现应各自独立 comment，因此去重粒度是「同文件+同行+同描述前40字」，
    而不是「同文件+同行」，避免同行第二条不同发现被误判为"已报告"而跳过。

    用 emoji+severity 格式识别我们的 AI inline comment，而非 Agent 名称
    （body 里不包含 Agent 名称）。
    """
    reported: set[tuple] = set()
    file_line_re = re.compile(r"`([^`:\n]+):(\d+)`")
    for c in comments:
        body = c.get("body", "") or ""
        if not _AI_INLINE_RE.search(body):
            continue
        m = file_line_re.search(body)
        if not m:
            continue
        fname = m.group(1)
        line = int(m.group(2))
        # 提取描述文本：标题行（emoji+severity+location）之后第一行非空文本
        desc_snippet = ""
        for ln in body.split("\n")[2:]:
            stripped = ln.strip()
            if stripped and not stripped.startswith("`") and not stripped.startswith("```"):
                desc_snippet = stripped[:40]
                break
        reported.add((fname, line, desc_snippet))
    return reported


def _parse_run_count(body: str) -> int:
    """从 MR 描述中解析已有 AI 检视轮次，默认 0。"""
    m = re.search(r"第\s*(\d+)\s*次", body)
    return int(m.group(1)) if m else 0


def _strip_ai_section(body: str) -> str:
    """移除描述中已有的 AI 检视段落（两个 HTML 注释标记之间）。"""
    start = body.find(_AI_SECTION_START)
    if start == -1:
        return body
    end = body.find(_AI_SECTION_END, start)
    after = body[end + len(_AI_SECTION_END):] if end != -1 else ""
    return (body[:start] + after).strip()


def _build_ai_section(
    summary: dict,
    all_findings: list[dict],
    new_findings: list[dict],
    skipped_findings: list[dict],
    run_count: int,
    now_str: str,
    pr_stats: dict | None = None,
) -> str:
    """生成 AI 检视 Markdown 段落（含整体评估 + 问题清单）。"""
    risk = summary.get("risk_level", "MEDIUM")
    impact = summary.get("impact_analysis", "")
    risk_reason = summary.get("risk_reason", "")
    focus_points = summary.get("focus_points", [])
    total_files = summary.get("total_files", 0)
    pr_stats = pr_stats or {}

    sev_counts: dict[str, int] = {"CRITICAL": 0, "HIGH": 0, "MEDIUM": 0, "LOW": 0}
    suggestion_count = 0
    for f in all_findings:
        sev_counts[f.get("severity", "LOW")] += 1
        if f.get("suggestion_code") is not None:  # "" 也算（删除该行建议）
            suggestion_count += 1

    risk_emoji = _SEV_EMOJI.get(risk, "⚪")
    total_issues = len(all_findings)
    skipped_count = len(skipped_findings)
    new_count = len(new_findings)

    dist_str = (
        f"🔴×{sev_counts['CRITICAL']} "
        f"🟠×{sev_counts['HIGH']} "
        f"🟡×{sev_counts['MEDIUM']} "
        f"🔵×{sev_counts['LOW']}"
    )
    skip_note = f"（本次新增 {new_count}，跳过重复 {skipped_count}）" if skipped_count > 0 else ""
    focus_text = "\n".join(f"- {p}" for p in focus_points) if focus_points else "- 无特殊关注点"

    # 问题清单：所有 findings，已有评论的标注跳过
    skipped_keys = {(f.get("file", ""), f.get("line_start", 0)) for f in skipped_findings}
    issue_lines: list[str] = []
    for i, f in enumerate(all_findings, 1):
        sev = f.get("severity", "LOW")
        emoji = _SEV_EMOJI.get(sev, "⚪")
        desc = f.get("description", "")
        if len(desc) > 60:
            desc = desc[:57] + "..."
        fname = f.get("file", "")
        line_s = f.get("line_start", 0)
        location = f"`{fname}:{line_s}`" if fname and line_s else ""
        if (fname, line_s) in skipped_keys:
            note = " （已有评论，跳过重复发布）"
        elif fname and line_s:
            note = " （详细评论已发布在对应代码行）"
        else:
            note = ""
        issue_lines.append(f"{i}. {emoji} **[{sev}]** {desc} — {location}{note}")

    issue_section = "\n".join(issue_lines) if issue_lines else "- 本次未发现问题"

    # xl PR 警告（基于研究数据：>1000行缺陷检出率仅28%，建议拆分）
    xl_warning = ""
    if pr_stats.get("tier") == "xl":
        lines_added   = pr_stats.get("lines_added", 0)
        lines_removed = pr_stats.get("lines_removed", 0)
        files_count   = pr_stats.get("files", 0)
        xl_warning = (
            f"\n> ⚠️ **PR 规模过大**：本次变更共 {files_count} 个文件、"
            f"{lines_added} 行新增 / {lines_removed} 行删除。"
            f"过大的 PR 会显著降低检视质量，建议拆分为多个独立的小 PR。\n"
        )

    parts = [
        _AI_SECTION_START,
        f"## 🤖 AI 代码检视报告（第 {run_count} 次）",
        xl_warning,
        "### 📊 整体评估",
        "",
        "| 指标 | 详情 |",
        "|------|------|",
        f"| 风险等级 | {risk_emoji} **{risk}** |",
        f"| 变更文件 | {total_files} 个 |",
        f"| 发现问题 | **{total_issues} 个** {skip_note}|",
        f"| 严重程度分布 | {dist_str} |",
        f"| 代码建议 | {suggestion_count} 条 |",
        "",
        f"**影响分析：** {impact}",
        "",
        f"**风险原因：** {risk_reason}",
        "",
        "### 🔍 关注点",
        "",
        focus_text,
        "",
        f"### 📋 问题清单（共 {total_issues} 个）",
        "",
        issue_section,
        "",
        "---",
        f"*由 gitcode-reviewer 自动生成 · {now_str} · 第 {run_count} 次检视*",
        _AI_SECTION_END,
    ]
    return "\n".join(parts)


# ── 节点实现 ───────────────────────────────────────────────────────────────

_MAX_FILES_PER_TASK = 5   # large/xl PR 每个 Agent 任务的最大文件数

# 这些 Agent 在多文件任务中容易忽略较小文件，强制按文件拆批保证覆盖率
_SPLIT_BY_FILE_AGENTS = {"QualityAgent", "PerformanceAgent"}


def _enforce_tier_rules(tasks: list[dict], tier: str) -> list[dict]:
    """对 Supervisor 调度决策做结构性纠正（不限制 Agent 类型，只做文件分批）。

    medium/large/xl：QualityAgent / PerformanceAgent 超过 1 个文件时按文件拆批，
    避免模型将注意力集中在变更最多的文件、忽略较小文件的问题。
    small 不拆批：文件数 ≤3，上下文小，单任务可覆盖；且拆批会导致 checkpoint 失效。

    large / xl：所有 Agent 任务文件数超过上限时自动拆批。
    """
    split: list[dict] = []
    for task in tasks:
        files = task.get("files", [])
        agent_type = task.get("agent_type", "")

        # medium/large/xl：Quality/Performance 按文件拆批保证覆盖率
        if agent_type in _SPLIT_BY_FILE_AGENTS and len(files) > 1 and tier != "small":
            for f in files:
                split.append({**task, "files": [f]})
            continue

        # large/xl：其余 Agent 超过上限时拆批
        if tier in ("large", "xl") and len(files) > _MAX_FILES_PER_TASK:
            for i in range(0, len(files), _MAX_FILES_PER_TASK):
                split.append({**task, "files": files[i:i + _MAX_FILES_PER_TASK]})
            continue

        split.append(task)

    return split


async def supervisor_node(state: ReviewState) -> dict:
    iteration = state.get("iteration", 0)
    tier = state.get("pr_stats", {}).get("tier", "medium")

    if iteration == 0:
        # 首轮：规则引擎（确定性，0 LLM 成本）派遣 + LLM Advisor 注入 focus_hints
        files = state.get("file_list", [])
        languages = state.get("languages", [])
        pr_stats = state.get("pr_stats", {})

        base_tasks = _rule_engine_dispatch(files, languages, tier, pr_stats)
        # small PR：文件少、路径明确，规则引擎启发已足够，跳过 LLM Advisor 节省 ~20s
        if tier == "small":
            hints: dict[str, str] = {}
        else:
            hints = await get_focus_hints(files, languages, pr_stats, base_tasks)
        for t in base_tasks:
            t["focus_hint"] = hints.get(t["agent_type"], "")

        tasks = _enforce_tier_rules(base_tasks, tier)
        agent_names = sorted({t["agent_type"] for t in tasks})
        logger.info("supervisor[rule_engine] tier=%s agents=%s", tier, agent_names)
        return {
            "supervisor_action":    "DISPATCH",
            "agents_to_dispatch":   tasks,
            "supervisor_reasoning": [f"规则引擎首轮派遣（{tier}）: {agent_names}"],
            "pr_meta": state.get("pr_meta", {}),
        }

    # 后续轮：LLM 动态追查决策（评估是否有高风险 findings 需要深挖）
    decision = await run_supervisor(state)
    tasks = _enforce_tier_rules(decision.get("agents_to_dispatch", []), tier)
    logger.info(
        "supervisor[llm] tier=%s action=%s tasks=%d",
        tier, decision.get("action"), len(tasks),
    )
    return {
        "supervisor_action":    decision.get("action", "FINISH"),
        "agents_to_dispatch":   tasks,
        "supervisor_reasoning": [decision.get("reasoning", "")],
        "pr_meta": decision.get("pr_meta", state.get("pr_meta", {})),
    }


async def run_agents_node(state: ReviewState) -> dict:
    tasks = state.get("agents_to_dispatch", [])
    head_sha = state.get("head_sha", "")
    diffs = state.get("diffs", [])
    project_id = state["project_id"]
    mr_iid = state["mr_iid"]
    languages = state.get("languages", [])
    task_id = state.get("task_id", "")
    iteration = state.get("iteration", 0)

    # ── Step Checkpoint：首轮加载已完成 Agent 的结果 ─────────────────────────
    # 只在 iteration==0 时加载——后续轮是 Supervisor 主动追查，应强制重新执行。
    checkpoint: dict[str, list[dict]] = {}
    if task_id and iteration == 0:
        try:
            from src.db.repository import load_agent_findings
            checkpoint = await load_agent_findings(task_id)
            if checkpoint:
                logger.info(
                    "Checkpoint resume: %d agent(s) already done: %s",
                    len(checkpoint), list(checkpoint.keys()),
                )
        except Exception as e:
            logger.warning("load_agent_findings failed (checkpoint skipped): %s", e)

    # 统计每个 agent_type 在本轮的批次数（批次>1 时不做 checkpoint，避免并发写覆盖）
    agent_batch_counts: Counter = Counter(t.get("agent_type", "") for t in tasks)

    reviewable_names = {d.get("filename", "") for d in diffs}

    # 本次检视周期的文件内容缓存（跨 Agent 共享，避免同一文件被多个 Agent 重复拉取）
    # key: "{project_id}:{head_sha}:{file_path}"，value: 文件内容字符串
    file_cache: dict[str, str] = {}

    async def _run_one(task: dict) -> list[dict]:
        agent_type = task.get("agent_type", "")
        is_single_batch = agent_batch_counts[agent_type] == 1

        # ── Checkpoint 命中：跳过 LLM 调用，直接返回缓存结果 ──────────────
        if is_single_batch and agent_type in checkpoint:
            cached = checkpoint[agent_type]
            logger.info(
                "[%s] checkpoint hit: skipping LLM, using %d cached findings",
                agent_type, len(cached),
            )
            return cached

        agent_fn = _AGENT_MAP.get(agent_type)
        if not agent_fn:
            logger.warning("Unknown agent_type: %s", agent_type)
            return []

        files = task.get("files", [])
        new_files = _detect_new_files(diffs, set(files) & reviewable_names)
        full_task = {
            **task,
            "project_id":  project_id,
            "mr_iid":      mr_iid,
            "diff_slice":  _extract_diff_slice(diffs, files),
            "new_files":   new_files,
            "languages":   languages,
            "_file_cache": file_cache,   # 共享缓存，减少重复 HTTP 请求
        }

        start_ms = int(asyncio.get_event_loop().time() * 1000)
        findings: list[dict] = []
        agent_status = "completed"
        try:
            kwargs: dict = {}
            if task.get("model"):
                kwargs["model"] = task["model"]
            if task.get("max_iterations"):
                kwargs["max_iterations"] = task["max_iterations"]
            findings = await agent_fn(full_task, head_sha, **kwargs)
        except Exception as e:
            logger.error("[%s] agent error: %s", agent_type, e)
            agent_status = "failed"

        duration_ms = int(asyncio.get_event_loop().time() * 1000) - start_ms

        # ── Step Checkpoint 写入：Agent 完成后立即落库 ──────────────────────
        # 批次>1 的 Agent（如 large PR 拆批的 QualityAgent）不写 checkpoint，
        # 避免并发 gather 时多批次互相覆盖 findings。
        if task_id and is_single_batch:
            try:
                from src.db.repository import save_agent_result
                await save_agent_result(
                    task_id, agent_type, findings,
                    duration_ms=duration_ms,
                    status=agent_status,
                )
                logger.info(
                    "[%s] checkpoint saved: findings=%d duration_ms=%d status=%s",
                    agent_type, len(findings), duration_ms, agent_status,
                )
            except Exception as e:
                logger.warning("[%s] checkpoint save failed (non-critical): %s", agent_type, e)

        return findings

    results = await asyncio.gather(*[_run_one(t) for t in tasks], return_exceptions=True)

    all_findings: list[dict] = []
    for r in results:
        if isinstance(r, list):
            all_findings.extend(r)
        elif isinstance(r, Exception):
            logger.error("Agent gather exception: %s", r)

    return {
        "findings":  all_findings,
        "iteration": iteration + 1,
    }


async def summary_node(state: ReviewState) -> dict:
    # 使用 final_findings（经过 synthesize + critic 过滤后的结果），
    # 保证总结和问题清单一致，不会出现"关注点提到但清单为空"的矛盾。
    summary = await run_summary_agent(
        file_list=state.get("file_list", []),
        raw_diff=state.get("raw_diff", ""),
        findings=state.get("final_findings", []),
    )
    return {"summary": summary}


def _pick_better_finding(a: dict, b: dict) -> dict:
    """两条同行 finding 取其一：优先高 severity，同 severity 取有 suggestion_code 的，再取描述更长的。"""
    sa = _severity_order(a.get("severity", "LOW"))
    sb = _severity_order(b.get("severity", "LOW"))
    if sa != sb:
        return a if sa < sb else b
    has_a = bool(a.get("suggestion_code") is not None)
    has_b = bool(b.get("suggestion_code") is not None)
    if has_a != has_b:
        return a if has_a else b
    return a if len(a.get("description", "")) >= len(b.get("description", "")) else b


def synthesize_node(state: ReviewState) -> dict:
    """对 findings 去重，对齐业界标准（PR-Agent / CodeRabbit / Reviewdog）：

    - Step 1：同 Agent 同行只保留最高 severity 那条
    - Step 1.5：不同 Agent 近行（±2 行内），若关键词重叠度 ≥ 0.25，视为同一问题只保留一条
      （±2 行容差：不同 LLM 对同一代码段的行号估算可能相差 1-2 行）
      （优先高 severity；同 severity 取有 suggestion_code 的；再取描述更长的）
    - Step 2：跨 Agent 描述前 40 字相同 → 去重
    """
    # Step 1：同 Agent 同行只保留最高 severity
    by_agent_line: dict[tuple, dict] = {}
    for f in state.get("findings", []):
        key = (f.get("agent", ""), f.get("file", ""), f.get("line_start", 0))
        prev = by_agent_line.get(key)
        if prev is None or _severity_order(f.get("severity", "LOW")) < _severity_order(prev.get("severity", "LOW")):
            by_agent_line[key] = f

    # Step 1.5：不同 Agent 近行（±2 行）关键词高度重叠 → 合并为一条
    # 按文件分组后，对每个 finding 贪心寻找可合并的"近行同主题"finding
    # 用 (file, rep_line) 作为代表行，找到后归入同一桶
    buckets: list[list[dict]] = []  # 每个桶是一组"近行且同主题"的 findings
    _LINE_TOLERANCE = 2

    for f in by_agent_line.values():
        file_f = f.get("file", "")
        line_f = f.get("line_start", 0)
        desc_f = f.get("description", "")
        absorbed = False
        for bucket in buckets:
            rep = bucket[0]
            if rep.get("file", "") != file_f:
                continue
            if abs(rep.get("line_start", 0) - line_f) > _LINE_TOLERANCE:
                continue
            if _desc_should_merge(desc_f, rep.get("description", "")):
                bucket.append(f)
                absorbed = True
                break
        if not absorbed:
            buckets.append([f])

    merged: list[dict] = []
    for bucket in buckets:
        if len(bucket) == 1:
            merged.append(bucket[0])
        else:
            best = bucket[0]
            for f in bucket[1:]:
                best = _pick_better_finding(best, f)
            logger.debug(
                "synthesize: merged %d findings at %s:%s–%s → 1",
                len(bucket), best.get("file", ""), bucket[0].get("line_start", ""),
                bucket[-1].get("line_start", ""),
            )
            merged.append(best)

    # Step 2：跨 Agent 去除描述相同的重复（前 40 字匹配）
    seen: set[tuple] = set()
    unique: list[dict] = []
    for f in merged:
        dedup_key = (f.get("file", ""), f.get("line_start", 0), f.get("description", "")[:40])
        if dedup_key not in seen:
            seen.add(dedup_key)
            unique.append(f)

    final = sorted(unique, key=lambda f: _severity_order(f.get("severity", "LOW")))
    return {"final_findings": final}


def critic_node(state: ReviewState) -> dict:
    """质量过滤：
    1. 去掉描述过短（< 10 字）或没有文件信息的 finding。
    2. 去掉不对应本次 diff + 行的 finding（未修改的行，跳过不报）。
    3. 内容合理性检查：description 声称的关键词（如 print）与实际代码行不符时丢弃，
       避免 Agent 行号错误导致"指鹿为马"式评论。
    """
    diffs = state.get("diffs", [])

    def _on_changed_line(f: dict) -> bool:
        fname = f.get("file", "")
        line_start = f.get("line_start", 0)
        if not fname or not line_start:
            return False
        patch_text = _patch_text_for_file(diffs, fname)
        if not patch_text:
            return False
        return _nearest_added_line(patch_text, line_start) is not None

    def _content_plausible(f: dict) -> bool:
        fname = f.get("file", "")
        line_start = f.get("line_start", 0)
        line_end = f.get("line_end", 0) or line_start
        description = f.get("description", "")
        if not fname or not line_start:
            return True
        patch_text = _patch_text_for_file(diffs, fname)
        # 检查整个 [line_start, line_end] 区间，避免多行 finding 被误杀
        code_text = _get_range_text(patch_text, line_start, line_end)
        return _description_plausible(description, code_text)

    kept = []
    for finding in state.get("final_findings", []):
        if not finding.get("file"):
            continue
        if len(finding.get("description", "")) < 10:
            continue
        if not _on_changed_line(finding):
            logger.info(
                "critic_node: skip pre-existing finding %s:%s (not on diff + line)",
                finding.get("file", ""), finding.get("line_start", ""),
            )
            continue
        if not _content_plausible(finding):
            logger.info(
                "critic_node: skip implausible finding %s:%s (description/code mismatch)",
                finding.get("file", ""), finding.get("line_start", ""),
            )
            continue
        kept.append(finding)

    return {"final_findings": kept}


async def publish_node(state: ReviewState) -> dict:
    """将 final_findings + summary 写回 GitCode（跨轮去重 + 结构化描述）。"""
    gc = GitCodeClient(settings.GITCODE_BASE_URL, settings.GITCODE_TOKEN)
    project_id = state["project_id"]
    mr_iid = state["mr_iid"]
    head_sha = state.get("head_sha", "")
    diffs = state.get("diffs", [])
    final_findings = state.get("final_findings", [])
    summary = state.get("summary", {})

    # ── 1. 跨轮去重：获取已有 AI 评论，提取 (file, line_start) ─────────────
    existing_comments: list[dict] = []
    already_reported: set[tuple[str, int]] = set()
    try:
        existing_comments = await gc.get_pr_comments(project_id, mr_iid)
        already_reported = _parse_reported_keys(existing_comments)
        logger.info("Found %d already-reported locations from existing comments", len(already_reported))
    except Exception as e:
        logger.warning("get_pr_comments failed, skipping dedup: %s", e)

    def _reported_key(f: dict) -> tuple:
        return (f.get("file", ""), f.get("line_start", 0), f.get("description", "")[:40])

    new_findings = [f for f in final_findings if _reported_key(f) not in already_reported]
    skipped_findings = [f for f in final_findings if _reported_key(f) in already_reported]
    logger.info(
        "publish_node: total=%d new=%d skipped=%d",
        len(final_findings), len(new_findings), len(skipped_findings),
    )

    # ── 2. 只为新发现发布评论 ────────────────────────────────────────────────
    posted = 0
    for finding in new_findings:
        fname = finding.get("file", "")
        line_start = finding.get("line_start", 0)
        description = finding.get("description", "")
        suggestion = finding.get("suggestion_code", "")
        severity = finding.get("severity", "LOW")
        agent = finding.get("agent", "")

        sev_emoji = _SEV_EMOJI.get(severity, "⚪")
        body = f"{sev_emoji} **[{severity}]** `{fname}:{line_start}`\n\n{description}"
        if suggestion is not None:  # "" = 删除该行，也要发 suggestion block
            body += f"\n\n```suggestion\n{suggestion}\n```"

        patch_text = _patch_text_for_file(diffs, fname)
        # 找到 diff 里最近的 + 行，取其文件行号（不用 diff offset，
        # 因为 GitCode /files API 只返回部分 hunk，offset 会偏移）
        actual_line = _nearest_added_line(patch_text, line_start) if patch_text and line_start else None

        try:
            if actual_line and head_sha and fname:
                await gc.post_inline_comment(
                    project_id, mr_iid, body,
                    {"head_sha": head_sha, "new_path": fname, "new_line": actual_line},
                )
            else:
                await gc.post_mr_note(project_id, mr_iid, body)
            posted += 1
        except Exception as e:
            logger.error("publish comment failed (file=%s line=%s): %s", fname, line_start, e)
            try:
                await gc.post_mr_note(project_id, mr_iid, body)
                posted += 1
            except Exception as e2:
                logger.error("fallback note also failed: %s", e2)

    # ── 3. 将 AI 总结发布到评论区（复检时更新同一条评论，不覆盖 MR 描述）──────
    if summary:
        try:
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
            # 从已有评论里找旧的 AI 摘要（复用步骤 1 已取到的列表，节省一次请求）
            old_summary_comment = _find_ai_summary_comment(existing_comments)
            run_count = (
                _parse_run_count(old_summary_comment.get("body", "") or "")
                if old_summary_comment else 0
            ) + 1
            ai_section = _build_ai_section(
                summary=summary,
                all_findings=final_findings,
                new_findings=new_findings,
                skipped_findings=skipped_findings,
                run_count=run_count,
                now_str=now_str,
                pr_stats=state.get("pr_stats"),
            )
            if old_summary_comment:
                await gc.update_pr_comment(
                    project_id, mr_iid,
                    old_summary_comment["id"],
                    ai_section,
                )
                logger.info("Updated existing AI summary comment (run #%d)", run_count)
            else:
                await gc.post_mr_note(project_id, mr_iid, ai_section)
                logger.info("Posted new AI summary comment (run #%d)", run_count)
        except Exception as e:
            logger.error("post/update AI summary comment failed: %s", e)

        # ── 4. 打风险标签 ────────────────────────────────────────────────────
        risk = summary.get("risk_level", "MEDIUM")
        label_name = _RISK_LABEL.get(risk)
        if label_name:
            try:
                existing = await gc.get_repo_labels(project_id)
                label_names = {lb["name"] for lb in existing}
                label_ready = label_name in label_names
                if not label_ready:
                    color = _LABEL_COLOR.get(label_name, "6b7280")
                    label_ready = await gc.create_label(project_id, label_name, color)
                    if label_ready:
                        logger.info("Label '%s' auto-created in repo", label_name)
                    else:
                        logger.warning(
                            "Label '%s' could not be created (API rejected), skipping MR label",
                            label_name,
                        )
                if label_ready:
                    await gc.update_mr_label(project_id, mr_iid, [label_name])
            except Exception as e:
                logger.error("update_mr_label failed: %s", e)

    logger.info("publish_node done: posted=%d skipped=%d", posted, len(skipped_findings))
    return {}


# ── 路由函数 ────────────────────────────────────────────────────────────────

def _route_supervisor(state: ReviewState) -> str:
    if state.get("supervisor_action") == "FINISH":
        return "synthesize"
    if state.get("iteration", 0) >= 5:
        logger.warning("Max iterations reached, forcing synthesize")
        return "synthesize"
    return "run_agents"


# ── 图构建 ─────────────────────────────────────────────────────────────────

def _build_graph() -> StateGraph:
    g = StateGraph(ReviewState)
    g.add_node("supervisor", supervisor_node)
    g.add_node("run_agents", run_agents_node)
    g.add_node("summary", summary_node)
    g.add_node("synthesize", synthesize_node)
    g.add_node("critic", critic_node)
    g.add_node("publish", publish_node)

    g.set_entry_point("supervisor")
    g.add_conditional_edges("supervisor", _route_supervisor, {
        "run_agents": "run_agents",
        "synthesize": "synthesize",
    })
    g.add_edge("run_agents", "supervisor")
    g.add_edge("synthesize", "critic")
    g.add_edge("critic",     "summary")
    g.add_edge("summary",    "publish")
    g.add_edge("publish",    END)
    return g


_graph = _build_graph().compile()


# ── 对外入口 ───────────────────────────────────────────────────────────────

async def _write_review_metrics(
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


async def run_review_graph(project_id: str, mr_iid: int, commit_sha: str) -> None:
    """Webhook handler 调用的入口，拉取 diff 后启动 LangGraph。

    并发控制策略（双层）：
    1. per-MR 锁：同一 MR 的多次 push 顺序执行，避免并发发评论造成混乱
    2. 全局信号量：限制系统同时运行的检视任务总数，防止 LLM API 过载
    """
    gc = GitCodeClient(settings.GITCODE_BASE_URL, settings.GITCODE_TOKEN)

    try:
        diff_data = await gc.get_pr_diff(project_id, mr_iid)
    except Exception as e:
        logger.error("get_pr_diff failed for %s#%d: %s", project_id, mr_iid, e)
        return

    diffs_all = diff_data.get("diffs", [])
    diffs_reviewable, diffs_skipped = _filter_reviewable_diffs(diffs_all)
    if diffs_skipped:
        logger.info(
            "skip %d non-reviewable files: %s",
            len(diffs_skipped),
            [(s["file"], s["reason"]) for s in diffs_skipped],
        )

    pr_stats = _calc_pr_stats(diffs_reviewable)
    languages = _detect_languages(diffs_reviewable)
    logger.info(
        "PR stats: tier=%s files=%d lines_added=%d lines_removed=%d languages=%s reviewable=%s",
        pr_stats["tier"], pr_stats["files"], pr_stats["lines_added"], pr_stats["lines_removed"],
        languages, [d.get("filename") for d in diffs_reviewable],
    )

    # ── Step Checkpoint：创建或恢复任务（MYSQL_URL 为空时静默跳过）──────────
    task_id = ""
    if settings.MYSQL_URL:
        try:
            from src.db.repository import complete_task, create_or_get_task, fail_task
            task_id = await create_or_get_task(
                project_id, mr_iid, commit_sha,
                tier=pr_stats["tier"],
                languages=languages,
                total_files=len(diffs_reviewable),
            )
            logger.info("DB task created/resumed: task_id=%s", task_id)
        except Exception as e:
            logger.warning("DB unavailable, running without checkpoint: %s", e)

    initial: dict = {
        "project_id": project_id,
        "mr_iid":     mr_iid,
        "commit_sha": commit_sha,
        "task_id":    task_id,
        "raw_diff":   diff_data.get("diff", ""),
        "file_list":  [d.get("filename", "") for d in diffs_reviewable],
        "diffs":      diffs_reviewable,
        "pr_stats":   pr_stats,
        "languages":  languages,
        "head_sha":   diff_data.get("head_sha", ""),
        "base_sha":   diff_data.get("base_sha", ""),
        "iteration":            0,
        "supervisor_action":    "",
        "supervisor_reasoning": [],
        "pr_meta":              {},
        "agents_to_dispatch":   [],
        "findings":             [],
        "final_findings":       [],
        "summary":              {},
    }

    # 双层并发控制（Redis 分布式，支持多实例横向扩展）：
    # 1. per-MR 分布式锁：同一 MR 的检视严格串行，避免不同 commit 的检视同时刷评论
    # 2. 全局分布式信号量：跨所有实例限制总并发数，防止 LLM API 过载
    async with _distributed_mr_lock(project_id, mr_iid):
        async with _distributed_global_semaphore(settings.MAX_CONCURRENT_REVIEWS):
            logger.info(
                "review_graph start: project=%s mr=%d commit=%s task_id=%s languages=%s",
                project_id, mr_iid, commit_sha[:8], task_id or "none", languages,
            )
            start_time = asyncio.get_event_loop().time()
            try:
                final_state = await _graph.ainvoke(initial)
                total_ms = int((asyncio.get_event_loop().time() - start_time) * 1000)
                logger.info(
                    "review_graph completed: project=%s mr=%d total_ms=%d",
                    project_id, mr_iid, total_ms,
                )
                if task_id:
                    await complete_task(task_id)
                await _write_review_metrics(
                    project_id, mr_iid, commit_sha,
                    pr_stats, languages, final_state, total_ms,
                )
            except Exception as e:
                logger.error("review_graph failed for %s#%d: %s", project_id, mr_iid, e)
                if task_id:
                    try:
                        await fail_task(task_id, str(e))
                    except Exception as fe:
                        logger.warning("fail_task also failed: %s", fe)
