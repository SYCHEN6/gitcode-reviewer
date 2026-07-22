"""scripts/ 共享工具：避免每个脚本重复相同的样板代码。

用法：
    from _common import *

这会在当前脚本所在目录（..）执行以下操作：
  - 将项目根目录加入 sys.path
  - 在 Windows 上修复 GBK 终端编码为 UTF-8
  - 提供标准的 argparse 父解析器
"""

import argparse
import io
import sys
from pathlib import Path

# ── sys.path：确保可以从 scripts/ 目录导入 src ──────────────────────────────
_PROJECT_ROOT = Path(__file__).parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# ── Windows GBK 终端 UTF-8 修复 ─────────────────────────────────────────────
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")


# ── 标准 argparse 父解析器 ──────────────────────────────────────────────────

def base_arg_parser(description: str) -> argparse.ArgumentParser:
    """创建带有 --project-id 和 --mr-iid 标准参数的基础解析器。"""
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--project-id", required=True, help='项目 ID，如 "owner/repo"')
    parser.add_argument("--mr-iid", type=int, required=True, help="MR IID")
    return parser


__all__ = ["_PROJECT_ROOT", "base_arg_parser"]
