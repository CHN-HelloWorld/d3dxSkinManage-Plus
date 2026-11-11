"""更新工作进程请求与结果协议中的可移植路径。

请求位于源容器下，应用内路径相对于请求目录；应用外路径保留绝对位置。
"""

from __future__ import annotations

import os
from pathlib import Path

from src.utils.paths import to_stored_path

_PLAN_PATHS = ("old_path", "archive", "work_root", "container", "deploy_target", "deploy_work", "old_deployed_path")
_RESULT_PATHS = ("body", "deployment")


def store_update_plan(plan: dict) -> dict:
    """复制更新计划，将应用内源、容器和部署路径转换为相对请求目录的存储路径。"""
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
    """正文始终属于任务目录；应用内的暂存部署同样按任务目录保存。"""
    result = dict(prepared)
    if result.get("body"):
        result["body"] = Path(os.path.relpath(result["body"], directory)).as_posix()
    deployment = result.get("deployment")
    if isinstance(deployment, str) and deployment and not Path(to_stored_path(deployment)).is_absolute():
        result["deployment"] = Path(os.path.relpath(deployment, directory)).as_posix()
    return result


def load_update_result(data: dict, directory: Path) -> dict:
    """复制已存结果，以任务目录为基准还原正文与暂存部署的实际路径。"""
    result = dict(data)
    for key in _RESULT_PATHS:
        value = result.get(key)
        if isinstance(value, str) and value:
            result[key] = os.path.normpath(directory / value)
    return result
