"""验证独立数据目录启动器的路径隔离与参数检查。"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parents[2]
LAUNCHER = PROJECT / "scripts/launch/open_experience.py"


@pytest.mark.parametrize("relative", [None, "data", "script"])
@pytest.mark.parametrize("no_elevate", [False, True])
def test_experience_launcher_keeps_source_and_data_paths_across_relaunch(tmp_path, relative, no_elevate):
    """检查真实路径与提权命令，只替换会进入 GUI 事件循环的应用入口。"""
    data_root = tmp_path / "独立体验库 sample"
    data_root.mkdir()
    marker = data_root / "sample.txt"
    marker.write_text("保留已有数据", encoding="utf-8")
    probe = '''
import json
import runpy
import sys
from pathlib import Path
from types import SimpleNamespace


def inspect_environment():
    """输出应用入口收到的真实路径并返回可辨识的退出码。"""
    from src.core import mod_index
    from src.utils import paths
    from src.utils.elevation import get_relaunch_command

    print(json.dumps({
        "app": str(paths.get_app_dir()),
        "cwd": str(Path.cwd()),
        "data": str(paths.get_data_dir()),
        "config": str(paths.get_config_dir()),
        "resources": str(paths.get_resources_dir()),
        "paths": paths.__file__,
        "index": mod_index.__file__,
        "relaunch": get_relaunch_command(),
    }))
    return 7


sys.modules["run_app"] = SimpleNamespace(main=inspect_environment)
sys.argv = [sys.argv[1], "--data-root", *sys.argv[2:]]
runpy.run_path(sys.argv[0], run_name="__main__")
'''
    argument = data_root.name if relative == "data" else str(data_root)
    script = "scripts/launch/open_experience.py" if relative == "script" else str(LAUNCHER)
    working_dir = PROJECT if relative == "script" else tmp_path
    flags = ["--no-elevate"] if no_elevate else []
    result = subprocess.run(
        [sys.executable, "-X", "utf8", "-B", "-c", probe, script, argument, *flags],
        cwd=working_dir,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
    )

    assert result.returncode == 7, result.stderr
    values = json.loads(result.stdout)
    assert Path(values["app"]).resolve() == data_root
    assert Path(values["cwd"]).resolve() == data_root
    assert Path(values["data"]).resolve() == data_root / "data"
    assert Path(values["config"]).resolve() == data_root / "config"
    assert Path(values["resources"]).resolve() == PROJECT / "resources"
    assert Path(values["paths"]).resolve() == PROJECT / "src/utils/paths.py"
    assert Path(values["index"]).resolve() == PROJECT / "src/core/mod_index.py"
    program, arguments, working_dir = values["relaunch"]
    assert Path(program).resolve() == Path(sys.executable).resolve()
    assert arguments == [str(LAUNCHER), "--data-root", str(data_root), *flags]
    assert Path(working_dir).resolve() == data_root
    assert marker.read_text(encoding="utf-8") == "保留已有数据"


@pytest.mark.parametrize("target", [None, "missing", "file"])
def test_experience_launcher_requires_an_existing_data_directory(tmp_path, target):
    arguments = []
    if target is not None:
        path = tmp_path / target
        if target == "file":
            path.write_text("保留已有文件", encoding="utf-8")
        arguments = ["--data-root", str(path)]
    before = set(tmp_path.iterdir())

    result = subprocess.run(
        [sys.executable, "-X", "utf8", "-B", str(LAUNCHER), *arguments],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
    )

    assert result.returncode == 2
    assert "--data-root" in result.stderr
    assert "Traceback" not in result.stderr
    assert set(tmp_path.iterdir()) == before
