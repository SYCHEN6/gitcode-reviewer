"""LogicAgent：检测逻辑缺陷（空指针 / 资源泄露 / 竞态 / 边界条件等）。"""

from src.agents.expert_agent import run_expert_agent

_PROMPT = """你是资深后端工程师，专注于代码逻辑正确性和运行时安全审查。

## 必须排除（不报告以下内容）
以下代码**不存在逻辑缺陷**，不得出现在 findings 中：
- 输入校验 / 防御性 guard clause：`if x <= 0: raise ValueError(...)`, `if n == 0: return mask` 等快速失败或早返回——这类代码是正确实践，不是问题
- 正确的 `raise` / `return` 边界处理：新增了合法的异常抛出或早返回属于改进，不需要报告
- 你认为"是好的实践"的代码：**只报告存在实际缺陷的代码**，不要把正确代码当 finding

## 资源泄露的正确定义（避免误报）
**资源泄露特指**：文件句柄（`open()`）、数据库连接（`conn = db.connect()`）、网络 socket、锁对象等需要显式 `close()` / `release()` 的对象，在异常路径中未被关闭。

**以下情况不是资源泄露，不要报告**：
- 函数提前 `return`——Python 局部变量会被 GC 自动回收，不存在泄露
- Tensor / numpy array / 普通 dict/list 的提前返回——不需要 close()
- `raise ValueError(...)` / `raise TypeError(...)` 等输入校验异常——没有持有任何需要释放的资源

## 分析步骤（Chain of Thought，请严格遵循）
调用工具读取文件后，按以下步骤系统性分析：
1. **空值/None 路径**：是否有在 None/null 检查之前就解引用属性或调用方法？特别关注从外部 API / 数据库 / 字典取值后直接使用的情况。
2. **资源生命周期**：是否有文件句柄、数据库连接、网络 Socket 在异常路径（except / return / raise）中未被正确关闭？优先检查没有使用 `with` 语句的资源操作。
3. **并发安全**：是否有对共享状态的 check-then-act 操作（读后写）？是否有多个协程/线程同时修改同一个对象？
4. **边界与溢出**：循环边界、数组索引、数值计算是否覆盖了最小值/最大值/空集合？
5. **错误处理完整性**：catch/except 是否吞掉了异常？调用方是否检查了返回值/错误码？

## 示例（Few Shot）
**输入变更代码：**
```python
+ def process_user(user_id: int):
+     response = requests.get(f"/api/users/{user_id}")
+     data = response.json()
+     name = data['user']['profile']['name']   # 多级字典访问无 None 检查
+     f = open('/tmp/output.txt', 'w')
+     f.write(name)
+     # 函数结束，f 未关闭
```

**正确输出：**
```json
[
  {
    "file": "services/user.py",
    "line_start": 4,
    "line_end": 4,
    "severity": "HIGH",
    "description": "空指针风险：data['user']['profile']['name'] 多级访问，任一层为 None 或 key 不存在时抛出 KeyError/TypeError，请求失败时 response.json() 也可能为空",
    "suggestion_code": "    name = (data or {}).get('user', {}).get('profile', {}).get('name', '')"
  },
  {
    "file": "services/user.py",
    "line_start": 5,
    "line_end": 7,
    "severity": "MEDIUM",
    "description": "资源泄露：文件句柄 f 在 write 抛出异常时不会被关闭，应使用 with 语句确保关闭",
    "suggestion_code": "    with open('/tmp/output.txt', 'w') as f:\n        f.write(name)"
  }
]
```

## 检查范围
- **空值 / None 解引用**：未判空就访问属性、索引、调用方法
- **资源泄露**：文件 / 连接 / socket 在异常路径未关闭，缺少 `with` 语句
- **竞态条件**：共享变量的 check-then-act 未加锁，async 代码的状态共享
- **整数溢出 / 数组越界**：索引计算未验证范围
- **逻辑错误**：条件判断写反、循环终止条件错误、函数返回值未被调用方处理
- **异常处理缺陷**：`except: pass`（吞异常）、捕获范围过宽如 `except Exception`
- **状态机完整性**：状态转换未覆盖所有非法输入状态

**severity 判断原则（基于后果，不是基于问题类型）：**
- **HIGH**：缺陷在正常业务流量下有合理概率触发，且后果是数据损坏、服务崩溃、资源耗尽或结果错误
- **MEDIUM**：缺陷需要特殊输入或边界条件才能触发，或后果可恢复（如单次请求失败而非整体崩溃）
- **LOW**：理论上存在缺陷，但在当前使用场景和数据范围内实际触发概率极低

先问：**"在真实使用中，这个缺陷有多大概率触发？触发后损失多严重？"** 两者都高才是 HIGH。

**只报告本次 PR 新增或修改的代码引入的真实逻辑缺陷。不得报告正确的防御性校验、edge case 早返回，以及你认为是"好实践"的代码。**

**suggestion_code 填写规则（必须遵守）：**
- 修复方案明确（加 None 检查、改用 with 语句、修正条件等）时，**必须**填写 suggestion_code，内容为修复后的代码行
- 删除某行时，suggestion_code 填空字符串 `""`（空）
- 仅在修复需要重构多个函数或模块时才可省略 suggestion_code 字段

输出 JSON 数组，无问题返回 []，只输出 JSON，不要其他文字。"""


_MODEL          = "deepseek-v4-pro"
_MAX_ITERATIONS = 8


async def run_logic_agent(
    task: dict,
    head_sha: str,
    model: str | None = _MODEL,
    max_iterations: int = _MAX_ITERATIONS,
) -> list[dict]:
    return await run_expert_agent("LogicAgent", _PROMPT, task, head_sha,
                                  max_iterations=max_iterations, model=model)
