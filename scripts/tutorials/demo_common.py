"""教程工具共用的路径及隔离环境。"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
TUTORIAL = REPO / "docs" / "tutorials"
WORKSPACE = Path(tempfile.gettempdir()) / "d3dx-tutorial-placeholder"
MARKER = WORKSPACE / ".tutorial-workspace.json"
GAME_ID = "Sample Game"
DEMO_VERSION = "2026-10-30"


def require_workspace(*, create: bool = False) -> Path:
    """只接受带教程标记的临时目录，不接管其他目录中的数据。"""
    workspace = WORKSPACE.resolve()
    if workspace == REPO.resolve() or not workspace.is_relative_to(Path(tempfile.gettempdir()).resolve()):
        raise RuntimeError("演示工作区必须位于系统临时目录。")
    if MARKER.is_file():
        data = json.loads(MARKER.read_text(encoding="utf-8"))
        if data.get("kind") != "d3dx-tutorial-demo":
            raise RuntimeError("工作区标记不匹配。")
    elif create:
        if workspace.exists() and any(workspace.iterdir()):
            raise RuntimeError(f"目录已有非教程内容，已停止：{workspace}")
        workspace.mkdir(parents=True, exist_ok=True)
        MARKER.write_text(
            json.dumps({"kind": "d3dx-tutorial-demo", "version": DEMO_VERSION}, indent=2) + "\n",
            encoding="utf-8",
        )
    else:
        raise RuntimeError("请先运行 prep_workspace.py 创建演示数据。")
    return workspace


def isolate(*, create: bool = False) -> Path:
    """在导入业务模块之前把全部可写数据路径指向临时演示目录。"""
    workspace = require_workspace(create=create)
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    from src.utils import paths

    paths.get_app_dir = lambda: workspace
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    return workspace
