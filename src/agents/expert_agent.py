"""专家 Agent 公共实现：真正的 ReAct 循环（工具调用 → 读文件 → 推理 → 输出）。"""

import asyncio
import json
import logging
import re
import uuid

import httpx
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool as lc_tool
from langchain_openai import ChatOpenAI
from tenacity import AsyncRetrying, before_sleep_log, retry_if_exception, stop_after_attempt, wait_exponential

from src.config import settings
from src.tools.gitcode_client import GitCodeClient

logger = logging.getLogger(__name__)


def _is_retryable_llm_error(exc: BaseException) -> bool:
    """限流 / 服务端错误 / 网络错误 → 重试；4xx 鉴权/参数错误 → 不重试。"""
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in (429, 500, 502, 503, 504)
    if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError)):
        return True
    exc_type = type(exc).__name__
    if exc_type in ("RateLimitError", "APIConnectionError", "APITimeoutError"):
        return True
    if exc_type == "APIStatusError":
        return getattr(exc, "status_code", 0) in (429, 500, 502, 503, 504)
    return False


async def _llm_invoke_with_retry(llm, messages: list):
    """带指数退避重试的 LLM 调用（限流/服务错误最多重试 3 次，退避 2-30 秒）。"""
    async for attempt in AsyncRetrying(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        retry=retry_if_exception(_is_retryable_llm_error),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    ):
        with attempt:
            return await llm.ainvoke(messages)


_VALID_SEVERITIES = {"CRITICAL", "HIGH", "MEDIUM", "LOW"}


def _make_llm(model: str | None = None) -> ChatOpenAI:
    """根据模型名称自动选择对应的 API endpoint 和 key。
    deepseek-* 走 DeepSeek API，其余走 DashScope。
    """
    m = model or settings.LLM_MODEL
    if m.startswith("deepseek"):
        return ChatOpenAI(
            model=m,
            base_url=settings.DEEPSEEK_BASE_URL,
            api_key=settings.DEEPSEEK_API_KEY,
            temperature=0,
        )
    return ChatOpenAI(
        model=m,
        base_url=settings.DASHSCOPE_BASE_URL,
        api_key=settings.DASHSCOPE_API_KEY,
        temperature=0,
    )


def _parse_findings(raw: str, agent_type: str) -> list[dict]:
    """从 LLM 输出中提取 Finding JSON 数组，容错处理各种格式。"""
    text = raw.strip()

    # 优先提取 ```json [...] ``` 代码块
    m = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.DOTALL)
    if m:
        text = m.group(1)
    else:
        # 非贪婪匹配第一个完整 JSON 数组
        m = re.search(r"\[.*?\]", text, re.DOTALL)
        if m:
            text = m.group(0)
        else:
            # 最后尝试贪婪匹配（兜底）
            m = re.search(r"\[.*\]", text, re.DOTALL)
            if m:
                text = m.group(0)

    try:
        items = json.loads(text)
    except Exception:
        logger.warning("[%s] Failed to parse findings JSON: %s", agent_type, raw[:200])
        return []

    if not isinstance(items, list):
        return []

    result = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if not item.get("file") or not item.get("description"):
            continue
        severity = item.get("severity", "LOW").upper().strip()
        if severity not in _VALID_SEVERITIES:
            severity = "LOW"
        # suggestion_code: None = 未提供，"" = 删除该行，非空字符串 = 替换内容
        # 若模型填写的是纯文字说明而非代码，强制置为 None（避免以 suggestion block 渲染文字）
        raw_suggestion = item.get("suggestion_code")
        if isinstance(raw_suggestion, str) and raw_suggestion.strip():
            chinese_chars = sum(1 for c in raw_suggestion if "一" <= c <= "鿿")
            if chinese_chars > len(raw_suggestion) * 0.3:
                raw_suggestion = None
            # import 语句不应出现在 suggestion block（修复需放模块级，不适合内联建议）
            elif re.search(r"^\s*import\s+\w", raw_suggestion, re.MULTILINE):
                raw_suggestion = None
        result.append({
            "finding_id":      str(uuid.uuid4()),
            "agent":           agent_type,
            "severity":        severity,
            "category":        _category_of(agent_type),
            "file":            item.get("file", ""),
            "line_start":      int(item.get("line_start", 0) or 0),
            "line_end":        int(item.get("line_end", 0) or item.get("line_start", 0) or 0),
            "diff_position":   0,  # 由 publish_node 根据 diff 计算
            "description":     item.get("description", ""),
            "suggestion_code": raw_suggestion,  # None = 不发 suggestion block
            "norm_reference":  item.get("norm_reference", ""),
        })
    return result


def _category_of(agent_type: str) -> str:
    return {
        "SecurityAgent":    "security",
        "LogicAgent":       "logic",
        "QualityAgent":     "quality",
        "PerformanceAgent": "performance",
    }.get(agent_type, "quality")


_CONTEXT_PADDING = 20  # 行范围上下各扩展的行数


def _make_file_tool(
    gc: GitCodeClient,
    project_id: str,
    head_sha: str,
    file_cache: dict[str, str] | None = None,
):
    """创建文件读取工具，支持按行范围精确读取。file_cache 为跨 Agent 共享的内容缓存。"""

    @lc_tool
    async def get_file_content(file_path: str, start_line: int = 0, end_line: int = 0) -> str:
        """获取 PR 中指定文件的内容。

        优先使用 start_line / end_line 定向读取——当 diff 中某个 hunk 的行号已知时，
        直接指定行范围（各自额外扩展 20 行上下文），比读整个大文件更高效且不会被截断。

        Args:
            file_path:  相对于仓库根目录的文件路径，例如 src/layers/attn.py
            start_line: 起始行号（从 1 计，含），0 表示从文件开头
            end_line:   结束行号（含），0 表示读到文件末尾（但全文模式最多返回 200 行）
        """
        try:
            cache_key = f"{project_id}:{head_sha}:{file_path}"
            if file_cache is not None and cache_key in file_cache:
                content = file_cache[cache_key]
            else:
                data = await gc.get_file_content(project_id, file_path, head_sha)
                content = data.get("content", "")
                if file_cache is not None and content:
                    file_cache[cache_key] = content
            if not content:
                return f"[{file_path} 内容为空或文件不存在]"

            lines = content.splitlines()
            total = len(lines)

            if start_line > 0 or end_line > 0:
                # 行范围模式：精确读取指定区域，上下各加 padding
                s = max(0, (start_line - 1) - _CONTEXT_PADDING)
                e = min(total, (end_line if end_line > 0 else total) + _CONTEXT_PADDING)
                selected = lines[s:e]
                header = f"# {file_path} 第 {s+1}–{s+len(selected)} 行（共 {total} 行）\n"
                return header + "\n".join(
                    f"{s+i+1:4d} | {ln}" for i, ln in enumerate(selected)
                )
            else:
                # 全文模式：最多返回前 200 行，并提示总行数
                limit = 200
                selected = lines[:limit]
                suffix = f"\n# ... 文件共 {total} 行，已截取前 {limit} 行。如需查看特定区域请用 start_line/end_line 参数。" if total > limit else ""
                return "\n".join(f"{i+1:4d} | {ln}" for i, ln in enumerate(selected)) + suffix
        except Exception as e:
            return f"[获取 {file_path} 失败: {e}]"

    return get_file_content


def _parse_hunk_ranges(diff_slice: str) -> list[tuple[str, int, int]]:
    """从 diff 文本中解析所有 hunk 的文件名和新文件行范围。
    返回 [(filename, new_start, new_end), ...]。
    """
    ranges: list[tuple[str, int, int]] = []
    current_file = ""
    for line in diff_slice.splitlines():
        if line.startswith("+++ b/"):
            current_file = line[6:]
        elif line.startswith("+++ ") and not line.startswith("+++ b/"):
            current_file = line[4:]
        elif line.startswith("@@") and current_file:
            m = re.search(r"\+(\d+)(?:,(\d+))?", line)
            if m:
                start = int(m.group(1))
                count = int(m.group(2)) if m.group(2) else 1
                ranges.append((current_file, start, start + count - 1))
    return ranges


async def _prefetch_hunk_contexts(
    gc: GitCodeClient,
    project_id: str,
    head_sha: str,
    hunk_ranges: list[tuple[str, int, int]],
    file_cache: dict[str, str] | None = None,
) -> str:
    """并发拉取所有 hunk 区域的文件内容，返回拼接好的上下文字符串。

    先按文件名去重，每个文件只发一次 HTTP 请求（同一文件的多个 hunk 复用同一次拉取结果），
    再从文件全文中切出各 hunk 对应的行范围。结合跨 Agent 共享的 file_cache，
    同一文件整个检视周期内只拉取一次。
    """
    if not hunk_ranges:
        return ""

    # 收集所有唯一文件名（保持顺序，避免重复 HTTP 请求）
    seen: set[str] = set()
    unique_files: list[str] = []
    for fname, _, _ in hunk_ranges:
        if fname not in seen:
            seen.add(fname)
            unique_files.append(fname)

    async def _fetch_file(fname: str) -> str:
        cache_key = f"{project_id}:{head_sha}:{fname}"
        if file_cache is not None and cache_key in file_cache:
            return file_cache[cache_key]
        try:
            data = await gc.get_file_content(project_id, fname, head_sha)
            content = data.get("content", "")
            if file_cache is not None and content:
                file_cache[cache_key] = content
            return content
        except Exception as ex:
            logger.debug("[prefetch] %s 获取失败: %s", fname, ex)
            return ""

    # 并发拉取各唯一文件（每文件一次 HTTP 请求）
    file_contents = await asyncio.gather(*[_fetch_file(f) for f in unique_files])
    contents_map: dict[str, str] = dict(zip(unique_files, file_contents))

    # 从文件全文切出各 hunk 行范围
    sections: list[str] = []
    for fname, start, end in hunk_ranges:
        content = contents_map.get(fname, "")
        if not content:
            sections.append(f"# [{fname}:{start}-{end}] 内容为空或获取失败\n")
            continue
        all_lines = content.splitlines()
        total = len(all_lines)
        s = max(0, start - 1 - _CONTEXT_PADDING)
        e = min(total, end + _CONTEXT_PADDING)
        selected = all_lines[s:e]
        numbered = "\n".join(f"{s+i+1:4d} | {ln}" for i, ln in enumerate(selected))
        sections.append(f"# {fname} 第 {s+1}–{s+len(selected)} 行（共 {total} 行）\n{numbered}\n")

    return "\n".join(sections)


async def _prefetch_dir_listing(
    gc: GitCodeClient,
    project_id: str,
    head_sha: str,
    new_files: list[str],
) -> str:
    """为新增文件预取其目录中的现有文件列表，提供项目结构上下文。

    帮助 Agent 发现：同名文件冲突、功能重复实现、命名不一致等问题。
    仅在文件是本次 PR 新增（status=added）时调用。
    """
    if not new_files:
        return ""

    import os as _os

    async def _fetch_one(fpath: str) -> str:
        dir_path = _os.path.dirname(fpath)
        try:
            items = await gc.list_directory(project_id, dir_path, head_sha)
            if not items:
                return ""
            fname = _os.path.basename(fpath)
            names = sorted(
                item.get("name", "") or item.get("path", "")
                for item in items
                if isinstance(item, dict) and (item.get("name") or item.get("path"))
            )
            dir_label = dir_path or "（根目录）"
            file_list = "\n".join(
                f"  {'→ ' if n == fname else '  '}{n}"
                for n in names
            )
            return (
                f"# 新增文件 `{fpath}` 所在目录 `{dir_label}` 的现有文件（→ 为本次新增）：\n"
                f"{file_list}\n"
            )
        except Exception as ex:
            logger.debug("[dir listing] %s 获取失败: %s", fpath, ex)
            return ""

    results = await asyncio.gather(*[_fetch_one(f) for f in new_files])
    combined = "\n".join(r for r in results if r)
    return combined


async def run_expert_agent(
    agent_type: str,
    system_prompt: str,
    task: dict,
    head_sha: str,
    max_iterations: int = 8,
    model: str | None = None,
) -> list[dict]:
    """ReAct 循环实现。

    启动前并发预取所有 hunk 上下文 + 新增文件目录列表，Agent 无需消耗 iteration 拉取文件，
    可将 iteration 预算专注于推理。同时逐次累计 token 消耗并在完成时输出日志。
    """
    project_id: str = task["project_id"]
    files: list[str] = task.get("files", [])
    focus_hint: str = task.get("focus_hint", "")
    diff_slice: str = task.get("diff_slice", "")
    new_files: list[str] = task.get("new_files", [])
    languages: list[str] = task.get("languages", [])
    # 跨 Agent 共享的文件内容缓存（由 run_agents_node 创建，减少重复 HTTP 拉取）
    file_cache: dict[str, str] | None = task.get("_file_cache")

    gc = GitCodeClient(settings.GITCODE_BASE_URL, settings.GITCODE_TOKEN)

    # 并发预取：hunk 上下文 + 新增文件目录列表
    hunk_ranges = _parse_hunk_ranges(diff_slice)
    hunk_contexts, dir_listing = await asyncio.gather(
        _prefetch_hunk_contexts(gc, project_id, head_sha, hunk_ranges, file_cache),
        _prefetch_dir_listing(gc, project_id, head_sha, new_files),
    )
    logger.debug(
        "[%s] prefetched %d hunks, %d new files, diff=%d chars",
        agent_type, len(hunk_ranges), len(new_files), len(diff_slice),
    )

    file_tool = _make_file_tool(gc, project_id, head_sha, file_cache)
    llm = _make_llm(model).bind_tools([file_tool])

    files_list = "\n".join(f"- {f}" for f in files)
    diff_text = diff_slice if diff_slice else "（无 diff 片段）"
    hint_text = focus_hint if focus_hint else "无，请全面检查所有变更文件。"

    hunk_section = (
        f"\n\n## 各 hunk 周边代码上下文（已预取，含行号）\n\n{hunk_contexts}"
        if hunk_contexts else ""
    )

    dir_section = (
        f"\n\n## 新增文件的目录结构（帮助判断命名冲突 / 功能重复）\n\n{dir_listing}"
        if dir_listing else ""
    )

    # 多语言检视指引：动态注入，让 Agent 知道要按哪些语言规范检视
    if languages:
        lang_str = "、".join(languages)
        lang_section = (
            f"\n\n## 检视语言\n"
            f"本次 PR 变更文件涉及：**{lang_str}**\n"
            f"请针对每种语言应用对应的最佳实践和安全规范（包括但不限于语言特有的内存模型、"
            f"并发模式、资源管理方式、常见反模式）。"
            f"示例和代码建议（suggestion_code）须使用对应语言的语法。"
        )
    else:
        lang_section = ""

    initial_msg = HumanMessage(content=(
        f"需要你检视的变更文件列表：\n{files_list}\n\n"
        f"## diff（+ 行为新增，- 行为删除，无前缀为上下文）\n```diff\n{diff_text}\n```"
        f"{hunk_section}"
        f"{dir_section}"
        f"{lang_section}\n\n"
        f"专项提示：{hint_text}\n\n"
        "上方已提供所有 hunk 的周边代码——请直接基于 diff 和已预取的上下文进行分析。\n"
        "若需要查看 diff 未覆盖的其他代码区域，可调用 get_file_content(file, start_line, end_line)。\n\n"
        "**【重要】必须逐一检查文件列表中的每个文件**：\n"
        "- 无论某个文件变更行数多少，哪怕只有 1 行新增，也必须单独检查并输出发现（或确认无问题）\n"
        "- 不允许因为另一个文件问题更多就跳过其他文件——每个文件都要覆盖到\n\n"
        "**suggestion_code 填写规则（必须遵守）：**\n"
        "- 只能填写可以直接替换原代码行的**修复代码**，不能填写文字说明、中文描述或问题分析\n"
        "- 若没有明确的代码修复方案，必须省略该字段（设为 null），不要用文字占位\n"
        "- 删除某行时填空字符串 `\"\"`，替换时填修复后的完整代码行\n\n"
        "**line_start 填写规则（必须严格遵守）：**\n"
        "- `line_start` 必须填写 diff 中 `+` 行对应的**新文件行号**（精确到具体的 `+` 行）\n"
        "- 计算方法：`@@ -X,Y +Z,W @@` 表示新文件从第 Z 行开始；"
        "该 hunk 内每遇到一个非 `-` 行行号 +1，`+` 行的行号就是 `line_start`\n"
        "- **不允许**填写 context 行的行号；问题必须在 `+` 行上\n"
        "- **注释行（以 `#` 或 `//` 开头）不得作为 `line_start`**，即使该注释是 PR 新增的。\n"
        "  若问题涉及「注释 + 代码」组合（如 `# debug` 注释 + 下方的 print/计算），\n"
        "  `line_start` 填第一行**实际代码**的行号，`line_end` 覆盖所有相关行。\n\n"
        "无问题时输出 []。只输出 JSON，不要其他文字。"
    ))

    messages: list = [SystemMessage(content=system_prompt), initial_msg]

    # token 消耗统计（跨所有 iteration 累加）
    total_in_tokens = 0
    total_out_tokens = 0

    for i in range(max_iterations):
        try:
            response = await _llm_invoke_with_retry(llm, messages)
        except Exception as e:
            logger.error("[%s] LLM call failed after retries (iter=%d): %s", agent_type, i, e)
            break

        # 累计 token 消耗（usage_metadata 格式与 OpenAI 兼容接口一致）
        usage = getattr(response, "usage_metadata", None) or {}
        total_in_tokens  += usage.get("input_tokens",  0)
        total_out_tokens += usage.get("output_tokens", 0)

        messages.append(response)
        tool_calls = getattr(response, "tool_calls", []) or []

        if not tool_calls:
            # 没有工具调用 → Agent 完成推理，解析 findings
            findings = _parse_findings(response.content or "", agent_type)
            if findings or i > 0:
                logger.info(
                    "[%s] done: iter=%d findings=%d tokens(in=%d out=%d total=%d)",
                    agent_type, i + 1, len(findings),
                    total_in_tokens, total_out_tokens, total_in_tokens + total_out_tokens,
                )
                return findings
            # i == 0 且 findings 为空：可能模型不支持工具调用，降级为预取模式
            logger.info("[%s] No tool calls on first iteration (sufficient context), falling back to prefetch", agent_type)
            return await _run_prefetch_fallback(agent_type, system_prompt, task, head_sha, gc, model, file_cache)

        # 执行工具调用
        for tc in tool_calls:
            try:
                result = await file_tool.ainvoke(tc["args"])
            except Exception as e:
                result = f"[工具执行失败: {e}]"
            messages.append(ToolMessage(
                tool_call_id=tc["id"],
                content=str(result),
            ))

    # 超出最大轮次：从最后一条 AI 消息里尝试提取 findings
    for msg in reversed(messages):
        if hasattr(msg, "content") and msg.content and not getattr(msg, "tool_calls", None):
            findings = _parse_findings(msg.content, agent_type)
            if findings:
                logger.info(
                    "[%s] max_iter reached, extracted %d findings. tokens(in=%d out=%d)",
                    agent_type, len(findings), total_in_tokens, total_out_tokens,
                )
                return findings
    logger.warning(
        "[%s] Max iterations reached, no findings. tokens(in=%d out=%d)",
        agent_type, total_in_tokens, total_out_tokens,
    )
    return []


async def _run_prefetch_fallback(
    agent_type: str,
    system_prompt: str,
    task: dict,
    head_sha: str,
    gc: GitCodeClient,
    model: str | None = None,
    file_cache: dict[str, str] | None = None,
) -> list[dict]:
    """降级方案：预取所有文件内容后做单次 LLM 调用（工具调用不可用时使用）。"""
    project_id = task["project_id"]
    files = task.get("files", [])
    diff_slice = task.get("diff_slice", "")
    focus_hint = task.get("focus_hint", "")

    file_sections: list[str] = []
    for fpath in files[:8]:
        try:
            cache_key = f"{project_id}:{head_sha}:{fpath}"
            if file_cache is not None and cache_key in file_cache:
                content = file_cache[cache_key][:3000]
            else:
                data = await gc.get_file_content(project_id, fpath, head_sha)
                content = data.get("content", "")
                if file_cache is not None and content:
                    file_cache[cache_key] = content
                content = content[:3000]
            file_sections.append(f"### {fpath}\n```\n{content}\n```")
        except Exception as e:
            file_sections.append(f"### {fpath}\n[获取失败: {e}]")

    diff_text = diff_slice[:2000] if diff_slice else "（无）"
    files_text = "".join(file_sections) if file_sections else "（无文件内容）"
    user_content = (
        f"Diff 变更片段：\n```diff\n{diff_text}\n```\n\n"
        f"变更文件内容：\n{files_text}\n\n"
        f"专项提示：{focus_hint or '无'}\n\n"
        "输出 JSON 数组 findings，无问题返回 []，只输出 JSON。"
    )

    llm = _make_llm(model)
    try:
        resp = await _llm_invoke_with_retry(llm, [
            SystemMessage(content=system_prompt),
            HumanMessage(content=user_content),
        ])
        return _parse_findings(resp.content or "", agent_type)
    except Exception as e:
        logger.error("[%s] Prefetch fallback LLM call failed after retries: %s", agent_type, e)
        return []
