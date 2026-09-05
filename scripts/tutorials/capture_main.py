"""使用真实 PySide6 控件生成最终版教程截图，所有业务数据均位于临时演示目录。"""

from __future__ import annotations

import argparse
import json
import os
import time
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from demo_common import GAME_ID, TUTORIAL, isolate

os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["QT_LOGGING_RULES"] = "qt.multimedia.*=false;qt.qpa.*=false"


def pump(app, milliseconds: int = 400) -> None:
    """在有限时间内处理界面事件，让后台图片和布局完成。"""
    deadline = time.monotonic() + milliseconds / 1000
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)


def wait_until(app, condition, *, timeout: float = 30) -> None:
    """等待明确的后台完成条件，超时即中止截图。"""
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() >= deadline:
            raise TimeoutError("截图等待后台任务超时。")
        pump(app, 30)
    pump(app, 120)
