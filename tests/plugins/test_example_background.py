"""验证清单导出示例通过宿主任务队列执行多文件插件逻辑。"""

import csv

import pytest
from PySide6.QtWidgets import QFileDialog

from src.plugin_sdk import BackgroundTask
from tests.plugins.test_examples_integration import window as window
from tests.plugins.test_operations import library as library

pytestmark = pytest.mark.usefixtures("trusted_sample_plugins")


def test_inventory_exports_through_background_task(window, library, qtbot, monkeypatch, tmp_path):
    target = tmp_path / "inventory.csv"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args, **kwargs: (str(target), "CSV"))
    manager = window._plugin_manager
    task = manager.execute_command("example.mod-inventory.export")
    assert isinstance(task, BackgroundTask)
    qtbot.waitUntil(lambda: task.done)
    assert task.result.ok and task.result.value == str(target)
    with target.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert rows[0]["mod_id"] == library[1].mod_id
