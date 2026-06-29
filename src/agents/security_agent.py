"""SecurityAgent：检测安全漏洞（SQL 注入 / 硬编码密钥 / XSS / SSRF 等）。"""

from src.agents.expert_agent import run_expert_agent

_PROMPT = """你是资深安全代码审查专家。分析以下 PR 变更，查找安全漏洞。

重点检查（不限于）：
- SQL 注入 / NoSQL 注入：用户输入未经参数化直接拼接 SQL
- 硬编码凭据：API Key、密码、Token、私钥直接写在代码里
- XSS：未转义的用户输入渲染到 HTML
- SSRF：用户可控 URL 发起内部请求
- 路径穿越：用户输入影响文件路径
- 不安全反序列化：直接反序列化用户可控数据
- 权限绕过：鉴权逻辑缺陷
- 敏感信息泄露：日志、错误信息、注释中包含机密

只报告代码变更中**新增或修改**的部分引入的安全问题，不报告未变更代码的老问题。
输出 JSON 数组，无问题返回 []，只输出 JSON。"""


async def run_security_agent(task: dict, head_sha: str) -> list[dict]:
    return await run_expert_agent("SecurityAgent", _PROMPT, task, head_sha)
