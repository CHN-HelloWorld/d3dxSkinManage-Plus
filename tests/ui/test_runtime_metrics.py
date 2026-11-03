"""验证常驻指标有界保留，同时保持性能报告的 JSON 和增量采样契约。"""

import copy
import json
from types import SimpleNamespace

from PySide6.QtCore import QObject


def test_controller_histories_remain_bounded_json_lists(qapp):
    from src.ui.deployment_activation import DeploymentActivationController
    from src.ui.deployment_enable import EnablePublicationController
    from src.ui.library_discovery import LibraryDiscoveryController
    from src.ui.library_reconcile_worker import LibraryReconcileController
    from src.ui.overview_refresh import OverviewRefreshController

    parent = QObject()
    manager = SimpleNamespace(invalidate_operation_trust=lambda: None)
    probe = LibraryReconcileController(manager, busy=lambda: False, parent=parent)
    controllers = (
        DeploymentActivationController(manager, parent),
        EnablePublicationController(manager, lambda *_args: None, parent),
        probe,
        LibraryDiscoveryController(manager, probe, busy=lambda: False, consume=lambda *_args: None, parent=parent),
        OverviewRefreshController(manager, current=lambda: (), apply=lambda *_args: None, parent=parent),
    )
    try:
        for controller in controllers:
            for history in controller.metrics.values():
                if not isinstance(history, list):
                    continue
                for number in range(10000):
                    history.append(number)
                assert len(history) <= 2048
                assert history[-1] == 9999
                assert json.loads(json.dumps(history)) == list(history)
    finally:
        parent.deleteLater()


def test_metric_delta_survives_eviction_and_deepcopy():
    from src.utils.runtime_metrics import MetricSamples, dropped_metric_samples, metric_samples_since

    samples = MetricSamples(range(8), limit=8)
    before = copy.deepcopy(samples)
    samples.extend((8, 9, 10))
    assert metric_samples_since(samples, before) == [8, 9, 10]
    assert dropped_metric_samples(samples, before) == 0
    assert before == list(range(8))
    assert before.total_count == 8 and samples.total_count == 11
    samples.extend(range(11, 30))
    assert metric_samples_since(samples, before)[-1] == 29
    assert dropped_metric_samples(samples, before) == 22 - len(samples)


def test_metric_delta_accepts_existing_plain_list_snapshots():
    from src.utils.runtime_metrics import dropped_metric_samples, metric_samples_since

    assert metric_samples_since([1, 2, 3], [1]) == [2, 3]
    assert dropped_metric_samples([1, 2, 3], [1]) == 0


def test_discovery_capture_keeps_dynamically_recorded_timings_bounded(qapp, monkeypatch):
    """执行真实快照时间片，覆盖首次运行时才会写入的采样字段。"""
    from src.ui import library_discovery as module
    from src.ui.library_reconcile_worker import LibraryReconcileController

    parent = QObject()
    manager = SimpleNamespace(invalidate_operation_trust=lambda: None)
    probe = LibraryReconcileController(manager, busy=lambda: False, parent=parent)
    controller = module.LibraryDiscoveryController(
        manager, probe, busy=lambda: False, consume=lambda *_args: None, parent=parent
    )
    elapsed = [0.0]

    def capture():
        """每步跨过界面时间预算，不启动磁盘扫描或其他后台任务。"""
        while True:
            elapsed[0] += 0.009
            yield

    monkeypatch.setattr(module, "time", SimpleNamespace(perf_counter=lambda: elapsed[0]))
    monkeypatch.setattr(controller, "_current", lambda: True)
    controller._capture_iterator = capture()
    try:
        for _ in range(5000):
            controller._capture_slice()
            controller._capture_timer.stop()
        history = controller.metrics["capture_slice_ms"]
        assert len(history) <= 2048
        assert history.total_count == 5000
        assert all(8 <= sample <= 10 for sample in history)
    finally:
        controller._cancel_capture()
        parent.deleteLater()
