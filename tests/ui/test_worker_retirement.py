"""后台任务销毁中的回调释放不能再次删除正在析构的线程对象。"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("kind", ["phase", "preset", "disable", "deploy", "category_preset", "discovery"])
@pytest.mark.parametrize("owner_kind", ["object", "widget"])
@pytest.mark.parametrize("state", ["not_started", "finished"])
def test_worker_callbacks_can_release_the_last_owner_reference(kind, owner_kind, state):
    """原生重入析构会终止整个进程，需以退出码及子进程内的释放断言共同验证。"""
    probe = Path(__file__).with_name("_worker_retirement_probe.py")
    result = subprocess.run(
        [sys.executable, "-B", str(probe), kind, owner_kind, state],
        cwd=probe.parents[2],
        env=dict(os.environ, QT_QPA_PLATFORM="offscreen", PYTHONIOENCODING="utf-8"),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
    )
    assert result.returncode == 0, f"child exited {result.returncode:#x}\n{result.stdout}\n{result.stderr}"
