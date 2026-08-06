"""ExportProgressDialog 装配回归测试

复现并锁死：勾选「导出附带元数据」后 start_export 因 dialog 缺少 _include_meta
属性而抛 AttributeError 的回归。端到端测试绕过了 dialog 的 UI 装配，故此处单独覆盖。

注意：用假 worker 替身代替真 ExportWorkerThread(QThread)，避免在测试间遗留
未启动的 QThread / 悬挂信号连接，污染后续 UI 测试的事件循环。
"""

from __future__ import annotations

import pytest

import src.core.archive_concurrency as concurrency
import src.ui.dialogs.export_progress as ep
from src.core.config import Config
from src.core.mod_exporter import ExportItem
from src.ui.dialogs.export_progress import ExportProgressDialog


class _StubManager:
    def __init__(self, root):
        self._root = root

    def _get_library_root(self):
        return self._root


class _FakeSignal:
    def connect(self, *args, **kwargs):
        pass


class _FakeWorker:
    """轻量替身：捕获构造参数，不创建真 QThread、不开线程。"""

    last = None

    def __init__(
        self,
        items,
        dest_dir,
        fmt,
        preserve_tree,
        library_root,
        include_meta=False,
        strict_format=False,
        parent=None,
        *,
        max_workers=1,
    ):
        self.include_meta = include_meta
        self.strict_format = strict_format
        self.max_workers = max_workers
        self.progress_updated = _FakeSignal()
        self.item_done = _FakeSignal()
        self.finished_all = _FakeSignal()
        self.stage_progress = _FakeSignal()
        _FakeWorker.last = self

    def start(self):
        pass

    def isRunning(self):
        return False


def test_dialog_defines_include_meta_and_passes_it_to_worker(qtbot, tmp_path, monkeypatch, temp_data_dir):
    # 替换为假 worker：不创建真 QThread，杜绝跨测试事件循环污染
    monkeypatch.setattr(ep, "ExportWorkerThread", _FakeWorker)

    item = ExportItem(name="x", src_path=str(tmp_path / "x"))
    dlg = ExportProgressDialog(
        [],
        str(tmp_path / "out"),
        _StubManager(tmp_path / "lib"),
        export_items=[item],
    )
    qtbot.addWidget(dlg)

    # _include_meta 在 start_export 前必须已初始化，避免导出时访问不存在的属性。
    assert hasattr(dlg, "_include_meta")

    # traceback 路径：start_export 装配 worker 不抛 AttributeError，并把 include_meta 传下去
    dlg.start_export()
    assert _FakeWorker.last is not None
    assert _FakeWorker.last.include_meta == dlg._include_meta


@pytest.mark.parametrize("configured,expected", [(0, 3), (12, 12)])
def test_export_reads_worker_preference_when_batch_starts(
    qtbot,
    tmp_path,
    monkeypatch,
    temp_data_dir,
    configured,
    expected,
):
    monkeypatch.setattr(ep, "ExportWorkerThread", _FakeWorker)
    monkeypatch.setattr(concurrency, "recommended_archive_workers", lambda: 3)
    Config().set("export.parallel_workers", 1)
    dialog = ExportProgressDialog(
        [],
        str(tmp_path / "out"),
        _StubManager(tmp_path / "lib"),
        export_items=[ExportItem("x", str(tmp_path / "x"))],
    )
    qtbot.addWidget(dialog)

    # 控制器创建后的配置变化应应用于即将开始的批次。
    Config().set("export.parallel_workers", configured)
    dialog.start_export()

    assert _FakeWorker.last.max_workers == expected
