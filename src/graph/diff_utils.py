"""代码检视辅助工具：diff 解析、文件过滤、语言检测、行号匹配、去重算法。

所有函数均为纯函数，无副作用，不依赖 LangGraph State。
"""

import logging
import os
import re
from collections import Counter

from src.graph._filter_data import (
    SKIP_BASENAMES,
    SKIP_EXTENSIONS,
    SKIP_NAME_SUFFIXES,
    SKIP_PATH_SEGMENTS,
)

logger = logging.getLogger(__name__)

# ── 不可检视文件过滤（规则数据在 _filter_data.py 中定义）────────────────────
# SKIP_EXTENSIONS / SKIP_BASENAMES / SKIP_PATH_SEGMENTS / SKIP_NAME_SUFFIXES
# 均从 _filter_data.py 导入，编辑该文件即可调整过滤策略。

# ── Token 预算（对齐 PR-Agent 的 soft/hard threshold 策略）────────────────
_TOKEN_BUDGET = 188_000        # tokens 可用于 diff
_CHARS_PER_TOKEN = 4           # 保守估算（代码比英文散文更密，约 3-4）
_SOFT_THRESHOLD_CHARS = int(_TOKEN_BUDGET * _CHARS_PER_TOKEN * 0.85)  # ~640K
_HARD_THRESHOLD_CHARS = int(_TOKEN_BUDGET * _CHARS_PER_TOKEN)          # ~752K


# ── 评分 / 排序 ─────────────────────────────────────────────────────────────

def severity_order(s: str) -> int:
    return {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}.get(s, 4)


def desc_keywords(desc: str) -> frozenset[str]:
    """从描述中提取关键词（非停用词 token），用于跨 Agent 去重相似度计算。"""
    _DESC_STOP = frozenset({
        "的", "在", "中", "是", "不", "和", "有", "为", "这", "以", "但", "或", "且",
    })
    tokens = re.split(r"[^a-zA-Z0-9一-鿿]+", desc.lower())
    return frozenset(t for t in tokens if t and t not in _DESC_STOP)


def desc_overlap(d1: str, d2: str) -> float:
    """Jaccard 相似度（基于关键词 token 集合）。"""
    k1, k2 = desc_keywords(d1), desc_keywords(d2)
    if not k1 or not k2:
        return 0.0
    return len(k1 & k2) / len(k1 | k2)


def desc_should_merge(d1: str, d2: str) -> bool:
    """同一文件近行（±2 行）的两条 finding 是否应合并（视为同一问题）。"""
    k1, k2 = desc_keywords(d1), desc_keywords(d2)
    if not k1 or not k2:
        return False
    common = k1 & k2
    if not common:
        return False
    if len(common) / min(len(k1), len(k2)) >= 0.25:
        return True
    code_common = {t for t in common if re.match(r"[A-Za-z]", t) and len(t) >= 4}
    return bool(code_common)


# ── 文件过滤 ────────────────────────────────────────────────────────────────

def is_reviewable(filename: str, patch: str) -> tuple[bool, str]:
    """判断文件是否可检视，返回 (可检视, 跳过原因)。"""
    if not patch:
        return False, "no patch (binary or rename-only)"

    lower = filename.lower()
    _, ext = os.path.splitext(lower)
    basename = os.path.basename(lower)

    if ext in SKIP_EXTENSIONS:
        return False, f"extension {ext}"
    if basename in SKIP_BASENAMES:
        return False, "lock/generated filename"
    if any(lower.startswith(seg) or f"/{seg}" in lower for seg in SKIP_PATH_SEGMENTS):
        return False, "generated/vendor path"
    if any(lower.endswith(sfx) for sfx in SKIP_NAME_SUFFIXES):
        return False, "minified/generated suffix"

    return True, ""


def filter_reviewable_diffs(diffs: list[dict]) -> tuple[list[dict], list[dict]]:
    """两步过滤：扩展名/路径 + Token 预算（文件级跳过，不截断）。"""
    reviewable: list[dict] = []
    skipped: list[dict] = []
    total_chars = 0

    for d in diffs:
        fname = d.get("filename", "")
        patch = d.get("patch", "")
        if isinstance(patch, dict):
            patch = patch.get("diff", "")

        # 第一步：类型过滤
        ok, reason = is_reviewable(fname, patch)
        if not ok:
            skipped.append({"file": fname, "reason": reason})
            continue

        # 第二步：token 预算
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


def calc_pr_stats(diffs: list[dict]) -> dict:
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
    if files <= 3 and total_lines <= 50:
        tier = "small"
    elif files <= 10 and total_lines <= 200:
        tier = "medium"
    elif files <= 20 and total_lines <= 1000:
        tier = "large"
    else:
        tier = "xl"

    return {
        "files":         files,
        "lines_added":   lines_added,
        "lines_removed": lines_removed,
        "total_lines":   total_lines,
        "tier":          tier,
    }


# ── 语言检测 ────────────────────────────────────────────────────────────────

_EXT_LANG_MAP: dict[str, str] = {
    ".py": "Python", ".pyi": "Python",
    ".go": "Go",
    ".java": "Java", ".kt": "Kotlin", ".kts": "Kotlin",
    ".ts": "TypeScript", ".tsx": "TypeScript",
    ".js": "JavaScript", ".jsx": "JavaScript", ".mjs": "JavaScript", ".cjs": "JavaScript",
    ".rs": "Rust",
    ".cpp": "C++", ".cc": "C++", ".cxx": "C++", ".hpp": "C++", ".hxx": "C++",
    ".c": "C", ".h": "C",
    ".cs": "C#",
    ".rb": "Ruby",
    ".swift": "Swift",
    ".scala": "Scala",
    ".sql": "SQL",
    ".sh": "Shell", ".bash": "Shell", ".zsh": "Shell",
    ".yaml": "YAML", ".yml": "YAML",
    ".json": "JSON",
    ".xml": "XML", ".html": "HTML", ".css": "CSS", ".scss": "SCSS", ".less": "Less",
    ".proto": "Protobuf",
    ".tf": "Terraform", ".tfvars": "Terraform",
    ".dockerfile": "Dockerfile",
    ".md": "Markdown", ".rst": "reStructuredText",
    ".toml": "TOML", ".cfg": "INI", ".ini": "INI",
    ".lua": "Lua",
    ".php": "PHP",
    ".dart": "Dart",
    ".r": "R",
    ".ex": "Elixir", ".exs": "Elixir",
    ".erl": "Erlang",
    ".hs": "Haskell",
    ".clj": "Clojure", ".cljs": "ClojureScript", ".cljc": "Clojure",
    ".sol": "Solidity",
    ".vue": "Vue",
    ".svelte": "Svelte",
    ".graphql": "GraphQL", ".gql": "GraphQL",
}


def detect_languages(diffs: list[dict]) -> list[str]:
    """从变更文件扩展名自动识别编程语言（去重排序）。"""
    counts: Counter = Counter()
    for d in diffs:
        fname = d.get("filename", "")
        _, ext = os.path.splitext(fname.lower())
        # .dockerfile 等非标准扩展名按文件名检测
        if fname.lower().endswith("dockerfile") or fname.lower() == "dockerfile":
            counts["Dockerfile"] += 1
            continue
        lang = _EXT_LANG_MAP.get(ext)
        if lang:
            counts[lang] += 1
    # 按出现次数降序，次数相同按字母序
    return sorted(counts.keys(), key=lambda k: (-counts[k], k))


def detect_new_files(diffs: list[dict], reviewable_set: set[str]) -> list[str]:
    """返回 reviewable 中 status=added 的文件路径列表。"""
    result = []
    for d in diffs:
        fname = d.get("filename", "")
        if fname in reviewable_set and d.get("status") == "added":
            result.append(fname)
    return result


# ── Diff 切片 ───────────────────────────────────────────────────────────────

def extract_diff_slice(diffs: list[dict], files: list[str]) -> str:
    """提取指定文件集合的完整 diff，按文件粒度拼接，不做字符截断。"""
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


# ── Diff 预解析索引（P2 优化：一次解析，多处复用）────────────────────────────

class DiffIndex:
    """预解析 diff 内容，缓存 (文件名 → 行信息) 映射。

    替代原先的多次独立解析（_patch_text_for_file, _nearest_added_line,
    _get_range_text 各自重复遍历完整 diff），现在只需构建一次 DiffIndex。
    """

    def __init__(self, diffs: list[dict]):
        """从 diff 列表构建索引。

        每个文件存储:
          - patch_text: 原始 diff patch 文本
          - entries: [(new_line_number, is_added, raw_line_text), ...]
        """
        self._by_file: dict[str, dict] = {}
        for d in diffs:
            fname = d.get("filename", "")
            patch = d.get("patch", "")
            if isinstance(patch, dict):
                patch = patch.get("diff", "")
            entries = self._parse_entries(patch)
            self._by_file[fname] = {
                "patch_text": patch or "",
                "entries": entries,
            }

    @staticmethod
    def _parse_entries(patch_text: str) -> list[tuple[int, bool, str]]:
        """解析 hunk，返回 [(new_line, is_added, raw_line), ...]。"""
        entries: list[tuple[int, bool, str]] = []
        new_line = 0
        in_hunk = False
        for raw_line in (patch_text or "").splitlines():
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
            entries.append((new_line, raw_line.startswith("+"), raw_line))
        return entries

    def patch_text_for(self, filename: str) -> str:
        """获取文件的原始 patch 文本。"""
        entry = self._by_file.get(filename)
        return entry["patch_text"] if entry else ""

    def nearest_added_line(self, patch_text: str, target_line: int) -> int | None:
        """若 target_line 本身是 diff 中的 + 行，返回该行号；否则返回 None。"""
        entry = self._by_file
        # 回退：如果传入 patch_text，通过 patch_text 内容匹配文件
        filename = None
        for fname, info in self._by_file.items():
            if info["patch_text"] == patch_text:
                filename = fname
                break

        if filename is None:
            # patch text 不匹配任何文件，做兼容回退解析
            entries = self._parse_entries(patch_text)
        else:
            entries = self._by_file[filename]["entries"]

        for ln, added, _ in entries:
            if ln == target_line and added:
                return ln
        return None

    def compute_diff_position(self, patch_text: str, target_line: int) -> int | None:
        """兼容旧调用：返回 patch 里 target_line 附近 + 行是否存在。"""
        return self.nearest_added_line(patch_text, target_line)

    def get_range_text(self, patch_text: str, line_start: int, line_end: int) -> str:
        """返回 patch 里 [line_start, line_end] 区间内所有行的拼接文本（不含 +/空格前缀）。"""
        # 匹配文件
        entries = None
        for info in self._by_file.values():
            if info["patch_text"] == patch_text:
                entries = info["entries"]
                break
        if entries is None:
            entries = self._parse_entries(patch_text)

        collected: list[str] = []
        for ln, _, raw_line in entries:
            if line_start <= ln <= line_end:
                collected.append(raw_line[1:] if raw_line else "")
            elif ln > line_end:
                break
        return "\n".join(collected)


# ── 行号 / 代码位置 ─────────────────────────────────────────────────────────

# (description 正则, 代码中必须出现的字符串)
_DESC_CODE_CHECKS: list[tuple[str, str]] = [
    (r"\bprint\b",    "print("),
    (r"console\.log", "console.log"),
    (r"\bdebugger\b", "debugger"),
    (r"\bpprint\b",   "pprint("),
]


def nearest_added_line(patch_text: str, target_line: int) -> int | None:
    """若 target_line 本身是 diff 中的 + 行，返回该行号；否则返回 None。

    要求精确匹配：context 行、- 行、或不在 diff 中的行一律返回 None。
    """
    entries: list[tuple[int, bool]] = []
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

    for ln, added in entries:
        if ln == target_line and added:
            return ln
    return None


def compute_diff_position(patch_text: str, target_line: int) -> int | None:
    """兼容旧调用：返回 patch 里 target_line 附近 + 行是否存在。"""
    return nearest_added_line(patch_text, target_line)


def get_range_text(patch_text: str, line_start: int, line_end: int) -> str:
    """返回 patch 里 [line_start, line_end] 区间内所有行的拼接文本。"""
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


def description_plausible(description: str, code_text: str) -> bool:
    """检查 description 中声称的关键词是否出现在实际代码区间内。

    只做"反证"：description 说有 X 但代码里没有 X → 行号错误，丢弃。
    """
    if not code_text:
        return True
    desc_lower = description.lower()
    code_lower = code_text.lower()
    for desc_pattern, code_kw in _DESC_CODE_CHECKS:
        if re.search(desc_pattern, desc_lower) and code_kw not in code_lower:
            return False
    return True


def patch_text_for_file(diffs: list[dict], filename: str) -> str:
    """从 diffs 列表中根据文件名查找 patch 文本。"""
    for d in diffs:
        if d.get("filename") == filename:
            patch = d.get("patch", "")
            if isinstance(patch, dict):
                patch = patch.get("diff", "")
            return patch or ""
    return ""
