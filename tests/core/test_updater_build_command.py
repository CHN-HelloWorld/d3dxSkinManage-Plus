"""验证发行构建只请求原生更新器，同时保留开发时的完整构建入口。"""

from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "arguments,projects",
    [
        (["--updater-only"], ["updater/updater.vcxproj"]),
        ([], ["updater/updater.vcxproj", "updater/tools/capture_ui.vcxproj", "updater/tests/updater_tests.vcxproj"]),
    ],
)
def test_updater_build_selects_requested_projects(tmp_path, monkeypatch, arguments, projects):
    from updater.tools import build

    program_files = tmp_path / "Program Files"
    vswhere = program_files / "Microsoft Visual Studio/Installer/vswhere.exe"
    vswhere.parent.mkdir(parents=True)
    vswhere.touch()
    installation = program_files / "Visual Studio"
    (installation / "MSBuild/Microsoft/VC/v170/Platforms/x64/PlatformToolsets/v143").mkdir(parents=True)
    monkeypatch.setenv("ProgramFiles(x86)", str(program_files))
    monkeypatch.setattr(build.subprocess, "check_output", lambda *_args, **_kwargs: str(installation))
    commands = []

    def run_compiler(command, **options):
        """记录外部编译请求，避免依赖测试机上安装的 Visual Studio。"""
        assert options["check"] is True
        assert Path(options["cwd"]).is_dir()
        commands.append(command)

    monkeypatch.setattr(build.subprocess, "run", run_compiler)

    build.main(arguments)

    assert [command[1] for command in commands] == projects
    assert all("/p:Configuration=Release" in command and "/p:Platform=x64" in command for command in commands)
    assert all("/p:DeployUpdater=false" in command for command in commands)
