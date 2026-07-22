"""LangGraph ReviewOrchestrator State 定义。"""

import operator
from typing import Annotated, TypedDict


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
