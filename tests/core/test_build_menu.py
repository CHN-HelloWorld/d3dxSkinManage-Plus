"""验证构建菜单在不同控制台编码下的显示、参数透传和退出码。"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="需要 Windows cmd.exe")

MENU = Path(__file__).resolve().parents[2] / "构建工具.bat"


def _run_menu(tmp_path, code_page, arguments="", exit_code=0):
    """在独立控制台中运行菜单，用替代构建入口记录参数并返回指定退出码。"""
    project = tmp_path / "中文 project"
    project.mkdir()
    menu = project / MENU.name
    menu.write_bytes(MENU.read_bytes())

    # 替代构建入口只记录菜单传入的真实参数，避免执行编译、安装或清理。
    release = project / "scripts" / "release"
    release.mkdir(parents=True)
    (release / "build.py").write_text(
        "import json, os, sys\nprint(json.dumps(sys.argv[1:]))\nsys.exit(int(os.environ['D3DX_TEST_BUILD_EXIT']))\n",
        encoding="utf-8",
    )

    launcher = tmp_path / "launch.cmd"
    launcher.write_bytes(
        (
            "@echo off\r\n"
            f"chcp {code_page} >nul\r\n"
            f'"%ComSpec%" /d /s /c ""%D3DX_TEST_MENU%" {arguments}"\r\n'
            "exit /b %errorlevel%\r\n"
        ).encode("ascii")
    )
    env = dict(
        os.environ,
        PYTHON=sys.executable,
        D3DX_TEST_MENU=str(menu),
        D3DX_TEST_BUILD_EXIT=str(exit_code),
    )
    env.pop("D3DX_BUILD_MENU_UTF8", None)
    result = subprocess.run(
        [env["COMSPEC"], "/d", "/c", str(launcher)],
        cwd=tmp_path,
        env=env,
        input="0\r\n",
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
        # 隔离测试运行器的控制台编码，让子 CMD 按指定代码页启动。
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    return result, project


@pytest.mark.parametrize("code_page", [936, 65001])
def test_fresh_console_displays_every_menu_option(tmp_path, code_page):
    result, project = _run_menu(tmp_path, code_page)

    assert result.returncode == 0, result.stderr
    assert re.findall(r"^\s*\[(\d)\]", result.stdout, re.MULTILINE) == [
        "1",
        "2",
        "3",
        "4",
        "5",
        "6",
        "7",
        "8",
        "9",
        "0",
    ], result.stdout + result.stderr
    assert str(project / "build") in result.stdout
    # timeout.exe 退出时可能报告输入被重定向，但 CMD 命令解析必须成功。
    assert not re.search(r"not recognized|不是内部或外部命令", result.stderr)


@pytest.mark.parametrize("exit_code", [0, 23])
def test_passthrough_preserves_quoted_arguments_and_exit_code(tmp_path, exit_code):
    result, _ = _run_menu(
        tmp_path,
        936,
        '--onefile --with-updater --zip -- --output-filename="menu smoke.exe"',
        exit_code,
    )

    assert result.returncode == exit_code, result.stdout + result.stderr
    assert result.stderr == ""
    assert json.loads(result.stdout) == [
        "--onefile",
        "--with-updater",
        "--zip",
        "--",
        "--output-filename=menu smoke.exe",
    ]
