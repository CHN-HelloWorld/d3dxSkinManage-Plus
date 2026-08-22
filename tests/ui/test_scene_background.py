import ctypes
import json
import sys
import threading
from types import SimpleNamespace

import pytest
import shiboken6
from PySide6.QtCore import QObject


def project(tmp_path, **changes):
    (tmp_path / "scene.pkg").write_bytes(b"scene")
    data = {"type": "scene", "file": "scene.pkg"}
    data.update(changes)
    path = tmp_path / "project.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_scene_requires_actual_contained_scene_file(tmp_path):
    from src.ui.backgrounds.scene import validate_scene_project

    path = project(tmp_path)
    assert validate_scene_project(str(path)) == path.resolve()
    for changes in ({"type": "video"}, {"file": "../scene.pkg"}, {"file": "missing.pkg"}):
        project(tmp_path, **changes)
        with pytest.raises(ValueError):
            validate_scene_project(str(path))


def test_workshop_scene_json_can_be_packed_in_scene_pkg(tmp_path):
    from src.ui.backgrounds.scene import validate_scene_project

    path = project(tmp_path, file="scene.json")
    assert not (tmp_path / "scene.json").exists()
    assert validate_scene_project(str(path)) == path.resolve()
    (tmp_path / "scene.pkg").unlink()
    with pytest.raises(ValueError):
        validate_scene_project(str(path))
    (tmp_path / "scene.json").write_text("{}", encoding="utf-8")
    assert validate_scene_project(str(path)) == path.resolve()


@pytest.mark.parametrize("kind", ["Scene", "scene", "SCENE"])
def test_workshop_scene_type_is_case_insensitive(tmp_path, kind):
    from src.ui.backgrounds.scene import validate_scene_project

    path = project(tmp_path, type=kind, file="scene.json")
    assert validate_scene_project(str(path)) == path.resolve()


def test_frames_own_pixels_and_only_latest_is_consumed():
    from src.ui.backgrounds.scene import SceneSource

    source = SceneSource("unused", "unused")
    pixels = (ctypes.c_ubyte * 24)()
    pixels[2] = 200
    buffer = SimpleNamespace(ctypes=SimpleNamespace(data=ctypes.addressof(pixels)), strides=(12, 4, 1))
    frame = SimpleNamespace(frame_buffer=buffer, width=3, height=2)
    source._session.on_frame(frame, SimpleNamespace(stop=lambda: None))
    pixels[2] = 0
    image = source.take_frame()
    assert image.pixelColor(0, 0).red() == 200
    assert source.take_frame() is None
    source.stop()


def test_stop_and_parent_destruction_cancel_without_waiting(qapp, monkeypatch):
    from src.ui.backgrounds import scene

    entered = threading.Event()

    def run(session):
        entered.set()
        session.cancel.wait(2)
        session.closed.set()

    monkeypatch.setattr(scene._Session, "run", run)
    parent = QObject()
    source = scene.SceneSource("unused", "unused", parent)
    source.start((100, 100))
    assert entered.wait(1)
    shiboken6.delete(parent)
    assert source._session.cancel.wait(1)
    assert source._session.closed.wait(1)


def test_rejects_changed_engine_before_launch(monkeypatch):
    from src.ui.backgrounds import scene

    monkeypatch.setattr(scene, "validate_engine_path", lambda path: (False, "changed executable"))
    calls = []
    monkeypatch.setattr(scene.subprocess, "run", lambda *a, **kw: calls.append(a))
    session = scene._Session("unknown.exe", "unused", lambda *a: None)
    with pytest.raises(ValueError, match="changed executable"):
        session.command("openWallpaper")
    assert calls == []


@pytest.mark.skipif(sys.platform != "win32", reason="Windows window styles")
def test_scene_window_is_renderable_without_taskbar_entry(qtbot, qapp):
    if qapp.platformName() != "windows":
        pytest.skip("需要 Windows 原生 Qt 窗口句柄")

    from PySide6.QtWidgets import QWidget

    from src.ui.backgrounds.scene import _Windows

    window = QWidget()
    qtbot.addWidget(window)
    hwnd = int(window.winId())
    windows = _Windows()
    try:
        windows.resize(hwnd, (100, 80))
        style = windows.api.GetWindowLongW(hwnd, -20)
        assert not style & (0x80 | 0x08000000 | 0x40000)
        owner = windows.api.GetWindow(hwnd, 4)
        assert owner and not windows.api.IsWindowVisible(owner)
        assert windows.api.IsWindowVisible(hwnd)
        windows.resize(hwnd, (120, 90))
        assert windows.api.GetWindow(hwnd, 4) == owner
    finally:
        windows._set_owner(hwnd, -8, 0)
        windows.dispose()


def test_engine_command_requests_hidden_startup(monkeypatch):
    from src.ui.backgrounds import scene

    monkeypatch.setattr(scene, "validate_engine_path", lambda _: (True, ""))
    calls = []
    monkeypatch.setattr(scene.subprocess, "run", lambda *a, **kw: calls.append(kw))
    scene._Session("official.exe", "unused", lambda *a: None).command("openWallpaper")
    startup = calls[0]["startupinfo"]
    assert startup.dwFlags & scene.subprocess.STARTF_USESHOWWINDOW
    assert startup.wShowWindow == scene.subprocess.SW_HIDE


def test_volume_properties_target_only_owned_window_with_raw_json(monkeypatch):
    from src.ui.backgrounds import scene

    monkeypatch.setattr(scene, "validate_engine_path", lambda _: (True, ""))
    commands = []
    monkeypatch.setattr(scene.subprocess, "run", lambda command, **kw: commands.append(command))
    session = scene._Session("official.exe", "unused", lambda *a: None)
    session.apply_volume(37)
    command = commands[0]
    assert "-control applyProperties -location " + session.name in command
    assert '-properties RAW~({"volume":37})~END' in command
    assert '\\"' not in command


def test_scene_volume_changes_are_nonblocking_and_clamped(monkeypatch):
    from src.ui.backgrounds.scene import SceneSource

    source = SceneSource("unused", "unused")
    source.set_volume(150)
    assert source._session.volume == 100
    source.set_volume(-1)
    assert source._session.volume == 0
    assert source._thread is None
    source.stop()


def test_open_retries_once_with_same_name_and_cleans_late_window(tmp_path, monkeypatch):
    from src.ui.backgrounds import scene

    elapsed = [0.0]
    commands = []
    closed = []
    errors = []

    class Windows:
        def dispose(self):
            pass

        def offscreen_x(self):
            return 4000

        def find(self, name):
            return 123 if elapsed[0] >= 20 else None

        def close(self, name):
            if self.find(name):
                closed.append(name)

    monkeypatch.setattr(scene, "_Windows", Windows)
    monkeypatch.setitem(sys.modules, "windows_capture", SimpleNamespace(WindowsCapture=object))
    monkeypatch.setattr(scene.time, "monotonic", lambda: elapsed[0])

    def advance(duration):
        elapsed[0] += duration

    monkeypatch.setattr(scene.time, "sleep", advance)
    session = scene._Session("official.exe", str(project(tmp_path)), lambda *a: errors.append(a))
    monkeypatch.setattr(session.cancel, "wait", lambda duration: advance(duration))
    # 失败后窗口才出现，此时清理逻辑正在跟踪待处理请求。
    monkeypatch.setattr(Windows, "find", lambda self, name: None)

    def command(*args, **kw):
        commands.append(args)
        if args[0] == "closeWallpaper":
            monkeypatch.setattr(Windows, "find", lambda self, name: 123)

    monkeypatch.setattr(session, "command", command)
    session.run()
    opened = [c for c in commands if c[0] == "openWallpaper"]
    assert len(opened) == 2
    assert opened[0] == opened[1]
    assert "-playInWindow" in opened[0]
    assert closed and set(closed) == {session.name}
    assert all(c == ("closeWallpaper", "-location", session.name) for c in commands if c[0] == "closeWallpaper")
    assert session.closed.is_set()
    assert any(kind == "error" for kind, _ in errors)


def test_cancel_pending_open_still_cleans_late_window(tmp_path, monkeypatch):
    from src.ui.backgrounds import scene

    elapsed = [0.0]
    closed = []

    class Windows:
        def dispose(self):
            pass

        def offscreen_x(self):
            return 4000

        def find(self, name):
            return 123 if elapsed[0] >= 0.5 else None

        def close(self, name):
            if self.find(name):
                closed.append(name)

    monkeypatch.setattr(scene, "_Windows", Windows)
    monkeypatch.setitem(sys.modules, "windows_capture", SimpleNamespace(WindowsCapture=object))
    monkeypatch.setattr(scene.time, "monotonic", lambda: elapsed[0])
    monkeypatch.setattr(scene.time, "sleep", lambda duration: elapsed.__setitem__(0, elapsed[0] + duration))
    session = scene._Session("official.exe", str(project(tmp_path)), lambda *a: None)

    def command(*args, **kw):
        if args[0] == "openWallpaper":
            session.cancel.set()

    monkeypatch.setattr(session, "command", command)
    session.run()
    assert closed and set(closed) == {session.name}
    assert elapsed[0] >= 20
    assert session.closed.is_set()


def test_application_quit_cancels_source(qapp):
    from src.ui.backgrounds.scene import SceneSource

    source = SceneSource("unused", "unused")
    qapp.aboutToQuit.emit()
    assert source._session.cancel.is_set()


def test_set_fps_changes_capture_throttle_without_restarting(monkeypatch):
    from src.ui.backgrounds import scene

    source = scene.SceneSource("unused", "unused")
    pixels = (ctypes.c_ubyte * 4)(0, 0, 200, 255)
    frame = SimpleNamespace(
        width=1,
        height=1,
        frame_buffer=SimpleNamespace(ctypes=SimpleNamespace(data=ctypes.addressof(pixels)), strides=(4, 4, 1)),
    )
    now = [10.0]
    monkeypatch.setattr(scene.time, "perf_counter", lambda: now[0])
    control = SimpleNamespace(stop=lambda: None)
    source._session.on_frame(frame, control)
    assert source.take_frame() is not None
    now[0] += 0.02
    source._session.on_frame(frame, control)
    assert source.take_frame() is None  # 每秒三十帧限制会丢弃过早到达的帧。
    name = source.window_name
    source.set_fps(60)
    source._session.on_frame(frame, control)
    assert source.take_frame() is not None
    assert source.window_name == name
    assert source._thread is None
    now[0] += 0.016  # 原生六十赫兹帧分发存在调度抖动。
    source._session.on_frame(frame, control)
    assert source.take_frame() is not None
    source.stop()


def test_capture_pacing_limits_native_readbacks_and_accounts_for_copy_time(monkeypatch):
    from src.ui.backgrounds import scene

    session = scene._Session("unused", "unused", lambda *a: None)
    session.fps = 30
    now = [10.0]
    waits = []
    monkeypatch.setattr(scene.time, "perf_counter", lambda: now[0])
    monkeypatch.setattr(session.cancel, "wait", lambda delay: waits.append(delay))

    def frame_arrived(frame, control):
        session.last_copy = now[0]
        now[0] += 0.010  # 像素复制已消耗本帧十毫秒预算。

    monkeypatch.setattr(session, "on_frame", frame_arrived)
    session.on_capture_frame(None, None)
    assert waits == pytest.approx([1 / 30 - 0.010])
    session.fps = 60
    session.on_capture_frame(None, None)
    assert waits[-1] == pytest.approx(1 / 60 - 0.010)
    session.cancel.set()
    session.on_capture_frame(None, None)
    assert len(waits) == 2


def test_padded_capture_rows_are_copied_without_retaining_native_pixels():
    import numpy as np

    from src.ui.backgrounds.scene import SceneSource

    backing = np.zeros((2, 256), dtype=np.uint8)
    pixels = backing[:, :12].reshape(2, 3, 4)
    pixels[0, 0] = (11, 22, 33, 255)
    pixels[1, 2] = (44, 55, 66, 255)
    source = SceneSource("unused", "unused")
    source._session.on_frame(SimpleNamespace(frame_buffer=pixels, width=3, height=2), None)
    image = source.take_frame()
    backing.fill(0)
    assert image.pixelColor(0, 0).getRgb() == (33, 22, 11, 255)
    assert image.pixelColor(2, 1).getRgb() == (66, 55, 44, 255)
    source.stop()


def test_stop_interrupts_capture_pacing(monkeypatch):
    from src.ui.backgrounds.scene import SceneSource

    source = SceneSource("unused", "unused")
    source.set_fps(1)
    pixels = (ctypes.c_ubyte * 4)(0, 0, 200, 255)
    frame = SimpleNamespace(
        width=1,
        height=1,
        frame_buffer=SimpleNamespace(ctypes=SimpleNamespace(data=ctypes.addressof(pixels)), strides=(4, 4, 1)),
    )
    waiting = threading.Event()
    wait = source._session.cancel.wait

    def paced_wait(delay):
        waiting.set()
        return wait(delay)

    monkeypatch.setattr(source._session.cancel, "wait", paced_wait)
    thread = threading.Thread(target=source._session.on_capture_frame, args=(frame, None))
    thread.start()
    try:
        assert waiting.wait(1)
        source.stop()
        thread.join(0.2)
        assert not thread.is_alive()
        assert source.take_frame() is None
    finally:
        source.stop()
        thread.join(2)
