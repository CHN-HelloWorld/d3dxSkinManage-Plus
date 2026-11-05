"""在独立进程中验证线程销毁不会重入销毁持有它的窗口。"""

from __future__ import annotations

import gc
import sys
import tempfile
import uuid
import weakref
from pathlib import Path

import shiboken6
from PySide6.QtCore import QEvent, QObject
from PySide6.QtWidgets import QApplication, QWidget

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.core.preset_deployment_plan import PresetDeploymentPlan
from src.ui.deploy_worker import DeployWorker
from src.ui.dialogs.category_preset import _PresetTask
from src.ui.disable_worker import DisableBatchWorker
from src.ui.library_discovery import DiscoveryRequest, DiscoveryWorker
from src.ui.preset_deploy_worker import DeploymentPhaseWorker, PresetDeployWorker


def make_worker(kind: str, owner: QObject):
    """建立无文件变更的真实线程；部署失败场景使用确定不存在的来源。"""
    if kind == "phase":
        return DeploymentPhaseWorker(lambda: None, owner)
    if kind == "disable":
        return DisableBatchWorker([], None, owner)
    if kind == "preset":
        plan = PresetDeploymentPlan("operation", "game", "before", "after", ())
        return PresetDeployWorker(plan, None, owner)
    if kind == "category_preset":
        return _PresetTask(lambda **_kwargs: None, owner)
    if kind == "discovery":
        root = Path(tempfile.gettempdir()) / ("d3dx-discovery-" + uuid.uuid4().hex)
        request = DiscoveryRequest("game", str(root), (), (), frozenset(), frozenset(), local_targets=())
        return DiscoveryWorker(request, owner)
    source = Path(tempfile.gettempdir()) / ("d3dx-retirement-" + uuid.uuid4().hex) / "missing.zip"
    return DeployWorker("00000000000040008000000000000001", str(source), None, owner)


def main() -> int:
    """让已结束或尚未启动的子线程持有宿主最后引用，再处理真实延迟销毁。"""
    kind, owner_kind, state = sys.argv[1:]
    app = QApplication([])
    app.setQuitOnLastWindowClosed(False)
    owner = QWidget() if owner_kind == "widget" else QObject()
    owner.setObjectName("owner")
    if isinstance(owner, QWidget):
        owner.show()
    worker = make_worker(kind, owner)
    calls = []
    worker.finished.connect(lambda kept=owner: calls.append(kept.objectName()))
    if state == "finished":
        worker.start()
        assert worker.wait(5000)
        app.processEvents()
        assert calls == ["owner"]
    assert worker.parent() is owner
    reference = weakref.ref(owner)
    worker.deleteLater()
    del owner
    assert reference() is not None
    app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()
    gc.collect()
    assert reference() is None
    assert not shiboken6.isValid(worker)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
