"""更新工作进程请求与结果协议中的可移植路径。

请求位于源容器下，受管路径相对于请求目录；部署路径保留绝对路径。
"""

from __future__ import annotations

import os
from pathlib import Path

from src.utils.paths import to_stored_path

_PLAN_PATHS = ("old_path", "archive", "work_root")


def store_update_plan(plan: dict) -> dict:
    """复制更新计划，将可随应用迁移的任务路径转换为相对工作目录的存储路径。"""
    result = dict(plan)
    root = Path(plan["work_root"])
    for key in _PLAN_PATHS:
        value = result.get(key)
        if isinstance(value, str) and value and not Path(to_stored_path(value)).is_absolute():
            result[key] = Path(os.path.relpath(value, root)).as_posix()
    return result


def load_update_plan(data: dict, directory: Path) -> dict:
    """复制持久计划，以任务目录为基准恢复其中的路径字段。"""
    result = dict(data)
    for key in _PLAN_PATHS:
        value = result.get(key)
        if isinstance(value, str) and value:
            result[key] = os.path.normpath(directory / value)
    return result


def store_update_result(prepared: dict, directory: Path) -> dict:
    """复制准备结果并将正文路径转换为任务目录内的相对路径。"""
    result = dict(prepared)
    if result.get("body"):
        result["body"] = Path(os.path.relpath(result["body"], directory)).as_posix()
    return result


def load_update_result(data: dict, directory: Path) -> dict:
    """复制已存结果，并将正文相对路径还原为任务目录下的实际路径。"""
    result = dict(data)
    if result.get("body"):
        result["body"] = os.path.normpath(directory / result["body"])
    return result
