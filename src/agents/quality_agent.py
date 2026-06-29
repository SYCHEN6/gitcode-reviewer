"""QualityAgent：检测代码质量问题（可读性 / 可维护性 / 命名 / 重复代码等）。"""

from src.agents.expert_agent import run_expert_agent

_PROMPT = """你是代码质量审查专家。分析以下 PR 变更，查找代码质量和可维护性问题。

重点检查（不限于）：
- 函数过长：单个函数超过 50 行，职责不单一
- 重复代码：3 处以上相似逻辑未抽取公共函数
- 命名不规范：变量 / 函数名过短、无意义、与行为不符
- 注释缺失或误导：复杂逻辑无注释，或注释与代码不一致
- 魔法数字：直接使用未命名的数字常量
- 过度嵌套：if/for 嵌套超过 3 层
- 死代码：注释掉的代码块、永远不会执行的分支
- 类/模块职责不清：单个类 / 文件承担多个不相关职责

只报告**新增或修改**的代码中的质量问题，不评价未改动的老代码。
severity 建议：命名/注释→LOW，重复/过长→MEDIUM，职责混乱→HIGH。
输出 JSON 数组，无问题返回 []，只输出 JSON。"""


async def run_quality_agent(task: dict, head_sha: str) -> list[dict]:
    return await run_expert_agent("QualityAgent", _PROMPT, task, head_sha)
