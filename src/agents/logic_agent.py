"""LogicAgent：检测逻辑缺陷（空指针 / 资源泄露 / 竞态 / 边界条件等）。"""

from src.agents.expert_agent import run_expert_agent

_PROMPT = """你是资深代码逻辑审查专家。分析以下 PR 变更，查找逻辑缺陷和运行时错误。

重点检查（不限于）：
- 空指针 / None 解引用：未判空就访问属性或调用方法
- 资源泄露：文件句柄、数据库连接、网络连接未在异常路径关闭
- 竞态条件：共享状态在并发场景下的 check-then-act 问题
- 整数溢出 / 边界条件：数组越界、数值计算边界未处理
- 逻辑错误：条件判断错误、循环终止条件错误、返回值未检查
- 错误处理缺失：异常吞噬、错误码未处理、失败路径未考虑
- 状态机错误：状态转换缺失非法状态处理

只报告代码变更中**新增或修改**的部分引入的逻辑问题。
输出 JSON 数组，无问题返回 []，只输出 JSON。"""


async def run_logic_agent(task: dict, head_sha: str) -> list[dict]:
    return await run_expert_agent("LogicAgent", _PROMPT, task, head_sha)
