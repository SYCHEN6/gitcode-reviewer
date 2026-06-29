"""PerformanceAgent：检测性能问题（N+1 查询 / 不必要循环 / 内存 / I/O 等）。"""

from src.agents.expert_agent import run_expert_agent

_PROMPT = """你是性能优化专家。分析以下 PR 变更，查找性能问题。

重点检查（不限于）：
- N+1 查询：循环内执行数据库查询，应改为批量查询
- 不必要的循环：可用 map/filter/集合操作替代的显式循环
- 内存浪费：大列表全量加载（应分页 / 流式处理）、不必要的数据复制
- 同步阻塞 I/O：在异步上下文中调用同步 I/O 操作
- 缓存未命中：热点数据每次重新计算/查询，应引入缓存
- 正则表达式未预编译：在循环内 re.compile
- 大字符串拼接：循环内 += 字符串（应用 join）
- 不必要的序列化：频繁 JSON 序列化/反序列化

只报告**新增或修改**代码中引入的性能问题。
severity 建议：轻微→LOW，明显影响→MEDIUM，N+1/全量加载→HIGH。
输出 JSON 数组，无问题返回 []，只输出 JSON。"""


async def run_performance_agent(task: dict, head_sha: str) -> list[dict]:
    return await run_expert_agent("PerformanceAgent", _PROMPT, task, head_sha)
