"""游戏启动保留 Windows 参数、程序路径与工作目录，并兼容原有纯路径配置。"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from src.core.migoto import MigotoIntegration


@pytest.fixture
def executable(tmp_path):
    """提供带空格和中文的程序路径；单元测试只替换实际进程创建边界。"""
    path = tmp_path / "Sample Game" / "程序.exe"
    path.parent.mkdir()
    path.write_bytes(b"test executable")
    return path


@pytest.mark.parametrize("quoted", [False, True])
@pytest.mark.parametrize(
    "arguments",
    ["", '--windowed --profile "Profile A"', r'--directory "D:\Data Folder\\" --text "\"quoted\"" --empty ""'],
)
def test_launch_preserves_arguments_and_uses_the_executable_directory(executable, monkeypatch, quoted, arguments):
    integration = MigotoIntegration()
    path_text = f'"{executable}"' if quoted else str(executable)
    integration.game_exe_path = path_text + (" " + arguments if arguments else "")
    calls, started = [], []
    monkeypatch.setattr(subprocess, "Popen", lambda command, **kwargs: calls.append((command, kwargs)))
    integration.game_started.connect(lambda: started.append(True))

    assert integration.launch_game()

    assert len(calls) == 1 and started == [True]
    command, options = calls[0]
    assert command == f'"{executable}"' + (" " + arguments if arguments else "")
    assert options["executable"] == str(executable)
    assert options["cwd"] == str(executable.parent)
    assert options["creationflags"] == subprocess.DETACHED_PROCESS
    assert not options.get("shell", False)


def test_launch_accepts_existing_path_objects(executable, monkeypatch):
    integration = MigotoIntegration()
    integration.game_exe_path = executable
    launched = []
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: launched.append(kwargs))

    assert integration.game_exe_path == executable
    assert integration.launch_game()
    assert launched[0]["executable"] == str(executable)


@pytest.mark.parametrize("invalid", ["empty", "missing", "unclosed_quote", "joined_arguments", "directory"])
def test_invalid_launch_command_does_not_start_a_process(executable, tmp_path, monkeypatch, invalid):
    integration = MigotoIntegration()
    integration.game_exe_path = {
        "empty": "",
        "missing": f'"{tmp_path / "Missing Game" / "missing.exe"}" --windowed',
        "unclosed_quote": f'"{executable} --windowed',
        "joined_arguments": f'"{executable}"--windowed',
        "directory": str(executable.parent),
    }[invalid]
    calls, started = [], []
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: calls.append(args))
    integration.game_started.connect(lambda: started.append(True))

    assert not integration.launch_game()
    assert calls == started == []


def test_process_creation_failure_does_not_emit_success(executable, monkeypatch):
    integration = MigotoIntegration()
    integration.game_exe_path = f'"{executable}" --windowed'
    started = []
    integration.game_started.connect(lambda: started.append(True))

    def fail_start(*args, **kwargs):
        """模拟程序被占用或不是有效 Windows 可执行文件。"""
        raise OSError("cannot start executable")

    monkeypatch.setattr(subprocess, "Popen", fail_start)
    assert not integration.launch_game()
    assert not started


@pytest.mark.skipif(os.name != "nt", reason="验证 Windows 原生进程的参数解析")
def test_real_process_receives_quoted_empty_unicode_and_literal_arguments(tmp_path, qtbot):
    script = tmp_path / "capture arguments.py"
    output = tmp_path / "arguments.json"
    script.write_text(
        "import json\nimport sys\nfrom pathlib import Path\n"
        "Path(sys.argv[1]).write_text(json.dumps(sys.argv[2:], ensure_ascii=False), encoding='utf-8')\n",
        encoding="utf-8",
    )
    expected = ["two words", 'embedded "quotes"', "D:\\Folder with space\\", "", "&", "$()", "示例参数"]
    integration = MigotoIntegration()
    integration.game_exe_path = subprocess.list2cmdline([sys.executable, str(script), str(output), *expected])

    assert integration.launch_game()

    def received_arguments():
        """等待独立进程完整写出结果，避免读取仍在写入的 JSON。"""
        try:
            return json.loads(output.read_text(encoding="utf-8")) == expected
        except (OSError, ValueError):
            return False

    qtbot.waitUntil(received_arguments, timeout=5000)
