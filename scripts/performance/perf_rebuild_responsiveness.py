"""使用隔离数据和 10 毫秒 Qt 心跳测量重建对话框各处理步骤。"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.performance.perf_mod_library import (
    GAME,
    build_library,
    emit,
    isolated_manager,
    mixed_deployments,
    percentiles,
)


def main():
    """在原生 Qt 窗口中对隔离库反复执行重建，采集阶段心跳及界面回调耗时并写出结果。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=5000)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument(
        "--mixed", action="store_true", help="Prepare four categories and 10%% real enabled deployments before timing"
    )
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    from src.ui.dialogs.rebuild_progress import RebuildProgressDialog

    app = QApplication.instance() or QApplication(sys.argv[:1])
    if app.platformName().lower() in {"offscreen", "minimal"}:
        raise RuntimeError("Formal rebuild acceptance requires the Windows GUI platform")
    results = []
    with isolated_manager(args.root) as manager:
        build_library(manager, args.root, args.count)
        mix = {"enabled_mods": 0}
        if args.mixed:
            manager.rebuild_index_from_filesystem(GAME)
            mix = mixed_deployments(manager, manager.get_mods(), args.root)
        for round_number in range(args.rounds):
            dialog = RebuildProgressDialog(manager, GAME)
            samples = defaultdict(list)
            callbacks = defaultdict(list)
            for name in ("_start", "_stage_next", "_commit", "_restored"):
                original = getattr(dialog, name)

                def measured(*values, _name=name, _original=original):
                    """调用原始重建阶段方法，并在成功或异常退出后都记录其执行时长。"""
                    start_call = time.perf_counter()
                    try:
                        return _original(*values)
                    finally:
                        callbacks[_name].append((time.perf_counter() - start_call) * 1000)

                setattr(dialog, name, measured)
            previous = [time.perf_counter(), dialog.phase]
            timer = QTimer()
            timer.setInterval(10)

            def tick():
                """将两次心跳间隔归入上一时刻的重建阶段，再更新采样起点。"""
                now = time.perf_counter()
                samples[previous[1]].append((now - previous[0]) * 1000)
                previous[:] = [now, dialog.phase]

            timer.timeout.connect(tick)
            timer.start()
            start = time.perf_counter()
            dialog.exec()
            tick()
            timer.stop()
            if dialog.error or dialog.report is None:
                raise RuntimeError(dialog.error or "no rebuild report")
            assert dialog.report.registered == args.count
            all_samples = [sample for phase in samples.values() for sample in phase]
            result = {
                "round": round_number + 1,
                "count": args.count,
                "enabled_mods": mix["enabled_mods"],
                "elapsed_s": time.perf_counter() - start,
                "heartbeat_ms": percentiles(all_samples),
                "owner_callbacks_ms": {
                    name: {"calls": len(values), **percentiles(values)} for name, values in callbacks.items()
                },
                "phases": {phase: {"samples": len(values), **percentiles(values)} for phase, values in samples.items()},
            }
            results.append(result)
            emit("rebuild_gui", **result)
            dialog.deleteLater()
            app.processEvents()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf8")


if __name__ == "__main__":
    main()
