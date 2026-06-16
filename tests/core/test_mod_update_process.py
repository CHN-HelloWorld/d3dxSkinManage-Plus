from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import textwrap
import time
import zipfile
from pathlib import Path

import pytest
from PySide6.QtTest import QSignalSpy

from tests.core.test_mod_update_transaction import _metadata
from tests.core.test_mod_update_transaction import update_case as update_case
from tests.helpers import registered_games as registered_games

_WRITER = r"""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

plan_path = Path(sys.argv[1])
plan = json.loads(plan_path.read_text(encoding="utf-8"))
root = plan_path.parent / plan["work_root"]
child = len(sys.argv) > 2
while not (root / "start").exists():
    time.sleep(0.005)

def publish(name, value):
    temporary = root / (name + ".tmp")
    temporary.write_text(json.dumps(value), encoding="utf-8")
    os.replace(temporary, root / name)

if not child and plan.get("descendant"):
    descendant = subprocess.Popen(
        [sys.executable, __file__, str(plan_path), "child"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    (root / "child.pid").write_text(str(descendant.pid), encoding="ascii")

name = "child.bin" if child else "parent.bin"
with (root / name).open("ab", buffering=0) as stream:
    count = 0
    while True:
        if (root / "abort-test").exists():
            sys.exit(9)
        stream.write(b"x" * 4096)
        count += 1
        if not child:
            publish("progress.json", {"phase": "extracting", "completed_files": count})
            if (root / "finish").exists():
                mode = plan.get("mode", "success")
                if mode == "crash":
                    sys.exit(7)
                if mode == "error":
                    publish("result.json", {"ok": False, "error": "archive is invalid"})
                    sys.exit(1)
                if mode == "malformed":
                    publish("result.json", ["unexpected", "result"])
                    sys.exit(0)
                if mode == "bad_success":
                    publish("result.json", {"ok": True, "result": ["not a mapping"]})
                    sys.exit(0)
                publish("result.json", {"ok": True, "result": {"prepared": "new-content"}})
                sys.exit(0)
        time.sleep(0.005)
"""


def _alive(pid: int) -> bool:
    if os.name == "nt":
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
        if not handle:
            return False
        try:
            return kernel.WaitForSingleObject(handle, 0) == 258  # WAIT_TIMEOUT
        finally:
            kernel.CloseHandle(handle)
    try:
        stat = Path(f"/proc/{pid}/stat")
        if stat.exists() and stat.read_text().rsplit(")", 1)[1].split()[0] == "Z":
            return False
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _wait_without_events(condition, timeout=5.0):
    """保留已退出进程的通知，等待下一次 Qt 定时器分发。"""
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "worker did not reach the requested state"
        time.sleep(0.01)


@pytest.fixture
def writer_factory(tmp_path, monkeypatch, qapp):
    from src.core.mod_update_process import UpdateProcess

    helper = tmp_path / "continuous_writer.py"
    helper.write_text(textwrap.dedent(_WRITER), encoding="utf-8")
    monkeypatch.setattr(
        UpdateProcess,
        "_worker_command",
        lambda self: [sys.executable, str(helper), str(self._work_root / "plan.json")],
    )
    processes = []

    def create(**options):
        work_root = tmp_path / f"work-{len(processes)}"
        work_root.mkdir()
        process = UpdateProcess({"work_root": str(work_root), **options})
        processes.append(process)
        return process, work_root

    yield create
    for process in processes:
        (process._work_root / "abort-test").touch()
        process.shutdown()


def test_pause_immediately_stops_parent_and_descendant_writes(writer_factory, qtbot):
    process, root = writer_factory(descendant=True)
    progress = QSignalSpy(process.progress)
    process.start()
    parent_file, child_file = root / "parent.bin", root / "child.bin"
    qtbot.waitUntil(lambda: child_file.exists() and child_file.stat().st_size > 4096, timeout=5000)
    qtbot.waitUntil(lambda: progress.count() > 0, timeout=5000)

    started = time.monotonic()
    assert process.pause()
    assert time.monotonic() - started < 0.75
    paused_sizes = (parent_file.stat().st_size, child_file.stat().st_size)
    paused_signals = progress.count()
    assert process.running and process.paused
    assert process.pause(), "repeated pause must not increment native suspend counts"
    qtbot.wait(200)
    assert (parent_file.stat().st_size, child_file.stat().st_size) == paused_sizes
    assert progress.count() == paused_signals

    assert process.resume()
    assert process.resume()
    qtbot.waitUntil(
        lambda: parent_file.stat().st_size > paused_sizes[0] and child_file.stat().st_size > paused_sizes[1],
        timeout=5000,
    )
    assert process.running and not process.paused
    qtbot.waitUntil(lambda: progress.count() > paused_signals, timeout=5000)


@pytest.mark.skipif(os.name != "nt", reason="Windows Python launchers can spawn before job assignment")
def test_slow_job_assignment_does_not_let_python_launcher_children_escape(writer_factory, qtbot, monkeypatch):
    from src.core.mod_update_process import _WindowsJob

    original_attach = _WindowsJob.attach

    def delayed_attach(job, pid):
        time.sleep(0.2)
        original_attach(job, pid)

    monkeypatch.setattr(_WindowsJob, "attach", delayed_attach)
    process, root = writer_factory(descendant=True)
    process.start()
    qtbot.waitUntil(lambda: (root / "child.bin").exists(), timeout=5000)
    assert process.pause()
    sizes = [(root / name).stat().st_size for name in ("parent.bin", "child.bin")]
    qtbot.wait(150)
    assert [(root / name).stat().st_size for name in ("parent.bin", "child.bin")] == sizes


@pytest.mark.parametrize("paused", [False, True])
def test_cancel_kills_descendants_before_emitting_canceled(writer_factory, qtbot, paused):
    process, root = writer_factory(descendant=True)
    canceled = QSignalSpy(process.canceled)
    succeeded = QSignalSpy(process.succeeded)
    failed = QSignalSpy(process.failed)
    process.start()
    qtbot.waitUntil(lambda: (root / "child.bin").exists(), timeout=5000)
    child_pid = int((root / "child.pid").read_text(encoding="ascii"))
    parent_pid = process.pid
    if paused:
        assert process.pause()
    stopped_sizes = [(root / name).stat().st_size for name in ("parent.bin", "child.bin")]
    started = time.monotonic()
    process.cancel()
    assert time.monotonic() - started < 0.75, "normal cancel must not wait for process cleanup"
    process.cancel()
    qtbot.waitUntil(lambda: canceled.count() == 1, timeout=5000)
    assert not _alive(parent_pid) and not _alive(child_pid)
    assert not process.running and not process.paused
    assert not process.resume()
    qtbot.wait(100)
    if paused:
        assert [(root / name).stat().st_size for name in ("parent.bin", "child.bin")] == stopped_sizes
    assert (succeeded.count() == 0) and (failed.count() == 0) and canceled.count() == 1


def test_result_and_progress_are_delivered_once_after_normal_exit(writer_factory, qtbot):
    process, root = writer_factory()
    progress, succeeded = QSignalSpy(process.progress), QSignalSpy(process.succeeded)
    process.start()
    qtbot.waitUntil(lambda: progress.count() > 0, timeout=5000)
    assert progress.at(progress.count() - 1)[0]["phase"] == "extracting"
    assert progress.at(progress.count() - 1)[0]["completed_files"] > 0
    (root / "finish").touch()
    qtbot.waitUntil(lambda: succeeded.count() == 1, timeout=5000)
    assert succeeded.at(0)[0] == {"prepared": "new-content"}
    assert not process.running
    process.start()
    process.cancel()
    qtbot.wait(100)
    assert succeeded.count() == 1


@pytest.mark.parametrize("cancel", [False, True])
def test_completed_process_waits_for_resume_and_cancellation_beats_late_result(writer_factory, qtbot, cancel):
    process, root = writer_factory()
    succeeded, canceled = QSignalSpy(process.succeeded), QSignalSpy(process.canceled)
    process.start()
    qtbot.waitUntil(lambda: (root / "parent.bin").exists(), timeout=5000)
    (root / "finish").touch()
    _wait_without_events(lambda: not _alive(process.pid))
    assert (root / "result.json").exists()
    assert succeeded.count() == 0
    assert process.pause(), "a pending completion must still be pausable"
    qtbot.wait(150)
    assert (succeeded.count() == 0) and process.running and process.paused
    if cancel:
        process.cancel()
        qtbot.waitUntil(lambda: canceled.count() == 1, timeout=5000)
        assert succeeded.count() == 0
    else:
        assert process.resume()
        qtbot.waitUntil(lambda: succeeded.count() == 1, timeout=5000)
        assert canceled.count() == 0
    assert not process.running


@pytest.mark.parametrize(
    ("mode", "expected"),
    [("error", "archive is invalid"), ("crash", "7"), ("malformed", "结果"), ("bad_success", "结果")],
)
def test_worker_failure_and_invalid_results_are_terminal(writer_factory, qtbot, mode, expected):
    process, root = writer_factory(mode=mode)
    failed, succeeded = QSignalSpy(process.failed), QSignalSpy(process.succeeded)
    (root / "finish").touch()
    process.start()
    qtbot.waitUntil(lambda: failed.count() == 1, timeout=5000)
    assert expected in failed.at(0)[0]
    assert (succeeded.count() == 0) and not process.running


def test_start_discards_stale_protocol_files(writer_factory, qtbot):
    process, root = writer_factory()
    (root / "start").touch()
    (root / "progress.json").write_text('{"phase": "stale"}', encoding="utf-8")
    (root / "result.json").write_text('{"ok": true, "result": {"stale": true}}', encoding="utf-8")
    succeeded, progress = QSignalSpy(process.succeeded), QSignalSpy(process.progress)
    process.start()
    qtbot.waitUntil(lambda: progress.count() > 0, timeout=5000)
    assert all(item[0]["phase"] == "extracting" for item in [progress.at(index) for index in range(progress.count())])
    assert succeeded.count() == 0
    stored = json.loads((root / "plan.json").read_text(encoding="utf-8"))["work_root"]
    assert (root / stored).resolve() == root.resolve()


def test_shutdown_reaps_a_suspended_process_tree(writer_factory, qtbot):
    process, root = writer_factory(descendant=True)
    canceled = QSignalSpy(process.canceled)
    process.start()
    qtbot.waitUntil(lambda: (root / "child.bin").exists(), timeout=5000)
    child_pid = int((root / "child.pid").read_text(encoding="ascii"))
    parent_pid = process.pid
    assert process.pause()
    process.shutdown()
    assert not process.running
    assert not _alive(parent_pid) and not _alive(child_pid)
    assert canceled.count() == 1


def test_shutdown_timeout_keeps_waiting_for_native_exit_before_cleanup(writer_factory, qtbot, monkeypatch):
    process, root = writer_factory()
    canceled = QSignalSpy(process.canceled)
    process.start()
    qtbot.waitUntil(lambda: (root / "parent.bin").exists(), timeout=5000)
    tree = process._tree
    root_exited = tree.root_exited
    # 模拟子进程收到终止请求后内核延迟回收的状态。
    monkeypatch.setattr(tree, "root_exited", lambda: False)
    process.shutdown()
    qtbot.wait(100)
    assert process.running and (canceled.count() == 0)
    monkeypatch.setattr(tree, "root_exited", root_exited)
    qtbot.waitUntil(lambda: canceled.count() == 1, timeout=5000)
    assert not process.running


def test_launch_failure_does_not_leave_a_running_task(writer_factory, monkeypatch):
    process, root = writer_factory()
    monkeypatch.setattr(process, "_worker_command", lambda: [str(root / "missing-executable")])
    failed = QSignalSpy(process.failed)
    process.start()
    assert not process.running and failed.count() == 1
    process.cancel()
    process.shutdown()
    assert failed.count() == 1


def test_progress_callback_can_pause_an_already_completed_worker(writer_factory, qtbot):
    process, root = writer_factory()
    succeeded = QSignalSpy(process.succeeded)
    process.start()
    _wait_without_events(lambda: (root / "parent.bin").exists())
    (root / "finish").touch()
    _wait_without_events(lambda: not _alive(process.pid))

    def pause_on_progress(snapshot):
        assert process.pause()

    process.progress.connect(pause_on_progress)
    qtbot.waitUntil(lambda: process.paused, timeout=5000)
    assert (succeeded.count() == 0) and process.running
    process.progress.disconnect(pause_on_progress)
    assert process.resume()
    qtbot.waitUntil(lambda: succeeded.count() == 1, timeout=5000)


def test_real_source_worker_prepares_and_commits_from_another_working_directory(update_case, qtbot, monkeypatch):
    from src.core.mod_update_process import UpdateProcess
    from src.core.mod_update_transaction import cleanup_update, commit_update, make_update_plan

    manager, old, archive = update_case
    original = Path(old.path).read_bytes()
    plan = make_update_plan(manager, old.mod_id, archive, _metadata("v2", 11))
    monkeypatch.chdir(Path(plan["work_root"]))
    process = UpdateProcess(plan)
    progress = QSignalSpy(process.progress)
    succeeded, failed = QSignalSpy(process.succeeded), QSignalSpy(process.failed)
    try:
        process.start()
        qtbot.waitUntil(lambda: succeeded.count() + failed.count() > 0, timeout=15000)
        assert [failed.at(index) for index in range(failed.count())] == []
        assert not process.running
        assert Path(old.path).read_bytes() == original
        prepared = succeeded.at(0)[0]
        stored_plan = json.loads((Path(plan["work_root"]) / "plan.json").read_text(encoding="utf-8"))
        assert not Path(stored_plan["old_path"]).is_absolute()
        assert not Path(stored_plan["work_root"]).is_absolute()
        assert not Path(stored_plan["archive"]).is_absolute()
        stored_result = json.loads((Path(plan["work_root"]) / "result.json").read_text(encoding="utf-8"))
        assert not Path(stored_result["result"]["body"]).is_absolute()
        assert Path(prepared["body"]).is_file()
        assert progress.count() > 0 and progress.at(progress.count() - 1)[0]["phase"] == "installing"
        updated = commit_update(manager, plan, prepared)
        assert updated.info.version == "v2"
        with zipfile.ZipFile(updated.path) as package:
            assert package.read("Skin/body.buf") == b"new body"
    finally:
        process.shutdown()
        # Windows 不允许删除进程当前工作目录。
        monkeypatch.chdir(archive.parent)
        cleanup_update(plan)


@pytest.mark.parametrize("packaging", ["source", "nuitka"])
def test_launch_command_selects_the_worker_entry_for_the_packaging_mode(tmp_path, qapp, monkeypatch, packaging):
    import src.core.mod_update_process as process_module
    from src.utils import paths

    if packaging == "nuitka":
        monkeypatch.setattr(paths, "__compiled__", object(), raising=False)
    process = process_module.UpdateProcess({"work_root": str(tmp_path)})
    command = process._worker_command()
    assert command[0] == sys.executable
    assert command[-1] == str(tmp_path / "plan.json")
    assert command[1:-1] == (["-m", "src.core.mod_update_worker"] if packaging == "source" else ["--mod-update-worker"])


@pytest.mark.parametrize("entry", ["run_app.py", "src/main.py"])
@pytest.mark.parametrize("has_plan", [False, True])
def test_worker_entry_runs_before_app_or_logging_imports(tmp_path, entry, has_plan):
    project_root = Path(__file__).resolve().parents[2]
    script = r"""
import importlib.abc
from pathlib import Path
import runpy
import sys
import types

root, entry, output, has_plan = sys.argv[1:]
sys.path.insert(0, root)
class ImportGuard(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in {"src.app", "src.utils.logger", "src.utils.win_console"}:
            raise AssertionError("worker imported application startup: " + fullname)
sys.meta_path.insert(0, ImportGuard())
worker = types.ModuleType("src.core.mod_update_worker")
def main(plan_path):
    Path(output).write_text(plan_path, encoding="utf-8")
    return 27
worker.main = main
sys.modules[worker.__name__] = worker
sys.argv = [entry, "--mod-update-worker"]
if has_plan == "yes":
    sys.argv.append("path with spaces/plan.json")
runpy.run_path(str(Path(root) / entry), run_name="__main__")
"""
    output = tmp_path / "called.txt"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            textwrap.dedent(script),
            str(project_root),
            entry,
            str(output),
            "yes" if has_plan else "no",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert result.returncode == (27 if has_plan else 2), result.stderr
    if has_plan:
        assert output.read_text(encoding="utf-8") == "path with spaces/plan.json"
    else:
        assert not output.exists()
