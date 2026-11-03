"""验证场景报告按累计位置提取指标，保留跨窗口的新增样本并标识截断。"""

import importlib.util
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.utils.runtime_metrics import MetricSamples


@pytest.mark.parametrize("added,expected,dropped", [(3, [8, 9, 10], 0), (20, list(range(20, 28)), 12)])
def test_scenario_samples_after_bounded_history_eviction(monkeypatch, added, expected, dropped):
    scripts = Path(__file__).parents[2] / "scripts" / "performance"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location("metric_window_scenarios", scripts / "perf_mod_library_scenarios.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    runner = module.ScenarioRunner.__new__(module.ScenarioRunner)
    samples = MetricSamples(range(8), limit=8)
    probe = dict(
        started=0, completed=0, discarded=0, max_active=1, snapshot_ms=samples, apply_slice_ms=[], slow_items=[]
    )
    discovery = dict(started=0, completed=0, discarded=0, capture_ms=[], worker_ms=[], planning_slice_ms=[])
    runner.window = SimpleNamespace(
        _library_probe_metrics=probe,
        _library_discovery=SimpleNamespace(metrics=discovery),
        _overview_refresh=SimpleNamespace(metrics=dict(completed=0, discarded=0, slice_ms=[])),
    )
    runner.samples, runner.phase_events = [], []
    runner.result = {"scenarios": []}
    runner.save = lambda: None
    runner.wait_idle = lambda _description: None

    def heartbeat(_predicate, _description, **_kwargs):
        """提供场景收尾所需心跳，避免用真实等待干扰指标窗口验证。"""
        runner.samples.append((time.perf_counter(), 16.0))

    def operation():
        """追加足以触发旧样本退役的真实指标。"""
        samples.extend(range(8, 8 + added))

    runner.wait_until = heartbeat
    runner.step("window", "test", operation)
    result = runner.result["scenarios"][0]
    assert result["periodic_snapshot_ms"] == expected
    assert result["metric_samples_dropped"]["periodic"].get("snapshot_ms", 0) == dropped
