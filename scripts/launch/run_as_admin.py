"""通过 Windows UAC 启动应用。

当前进程已经具有管理员权限时直接启动，否则创建独立的提权进程。
"""

from __future__ import annotations

import ctypes
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ENTRY = "src/main.py"


def _interpreter() -> Path:
    """优先使用项目虚拟环境，未找到时使用当前解释器。"""
    for rel in (".venv/Scripts/python.exe", ".venv/bin/python"):
        candidate = ROOT / rel
        if candidate.is_file():
            return candidate
    return Path(sys.executable)


def main() -> int:
    """选择应用解释器，已有权限时直接运行，否则通过 Windows UAC 创建提权子进程并报告启动结果。"""
    py = _interpreter()
    if sys.platform != "win32":
        return subprocess.call([str(py), ENTRY], cwd=str(ROOT))

    if ctypes.windll.shell32.IsUserAnAdmin():
        print(f"已是管理员，直接启动: {py} {ENTRY}")
        return subprocess.call([str(py), ENTRY], cwd=str(ROOT))

    print(f"申请提权启动（会弹 UAC）: {py} {ENTRY}")
    print(f"工作目录: {ROOT}")
    # 使用 runas 动作申请提权子进程，当前终端进程继续保留原有权限。
    rc = ctypes.windll.shell32.ShellExecuteW(
        None,
        "runas",
        str(py),
        ENTRY,
        str(ROOT),
        1,
    )
    if rc <= 32:  # ShellExecuteW 约定：返回值 <=32 即失败，5 = 用户拒绝了 UAC
        print(f"提权启动失败 (ShellExecuteW={rc}){'，你在 UAC 弹窗点了「否」' if rc == 5 else ''}")
        return 1
    print("已以管理员身份启动新进程；本终端不等待其退出。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
