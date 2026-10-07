"""通过实际命令行和安装路径验证插件校验与可复现打包。"""

import json
import subprocess
import sys
import zipfile

import pytest

from src.plugins.runtime.installer import InstallError
from tests.plugins.test_manifest_v2 import make_plugin
from tests.plugins.test_runtime import env as env


def run_tool(*arguments):
    """在独立进程中执行开发工具，捕获输出和退出状态。"""
    return subprocess.run(
        [sys.executable, "-X", "utf8", "scripts/plugins/plugin_tool.py", *map(str, arguments)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def test_package_is_reproducible_and_installed_relative_import_runs(env, tmp_path):
    manager, _, _ = env
    source = make_plugin(
        tmp_path / "source", files=["entry.py", "logic"], contributes={"commands": [{"id": "test.package.run"}]}
    )
    logic = source / "logic"
    logic.mkdir()
    (logic / "value.py").write_text("VALUE = 42\n", encoding="utf-8")
    (logic / "__pycache__").mkdir()
    (logic / "__pycache__/private.pyc").write_bytes(b"cache")
    (source / "secret.txt").write_text("private", encoding="utf-8")
    (source / "entry.py").write_text(
        """from .logic.value import VALUE
from src.plugin_sdk import PluginBase
class Plugin(PluginBase):
    def activate(self, ctx):
        ctx.commands.register(ctx.plugin_id + ".run", lambda: VALUE)
""",
        encoding="utf-8",
    )
    first, second = tmp_path / "first.zip", tmp_path / "second.zip"
    checked = run_tool("check", source, "--json")
    assert checked.returncode == 0, checked.stdout + checked.stderr
    assert json.loads(checked.stdout)["id"] == "test.package"
    for output in (first, second):
        result = run_tool("pack", source, "--output", output)
        assert result.returncode == 0, result.stdout + result.stderr
    assert first.read_bytes() == second.read_bytes()
    with zipfile.ZipFile(first) as archive:
        assert set(archive.namelist()) == {"plugin.json", "entry.py", "logic/value.py"}
    manager.install_archive(first)
    manager.trust("test.package")
    manager.enable("test.package")
    assert manager.get("test.package").instance is None
    assert manager.execute_command("test.package.run") == 42


def test_invalid_check_returns_machine_readable_error_without_executing_entry(tmp_path):
    source = make_plugin(tmp_path)
    (source / "entry.py").write_text(
        "import src.core.config\nraise RuntimeError('must not execute')\n", encoding="utf-8"
    )
    result = run_tool("check", source, "--json")
    assert result.returncode == 2
    report = json.loads(result.stdout)
    assert report["status"] == "error"
    assert "src.core.config" in report["error"]
    assert "must not execute" not in report["error"]


def test_package_requires_explicit_files_and_cannot_overwrite_its_source(tmp_path):
    source = make_plugin(tmp_path)
    output = tmp_path / "unsafe.zip"
    result = run_tool("pack", source, "--output", output)
    assert result.returncode == 2
    assert not output.exists()
    metadata = json.loads((source / "plugin.json").read_text("utf-8"))
    metadata["files"] = ["entry.py"]
    (source / "plugin.json").write_text(json.dumps(metadata), encoding="utf-8")
    before = (source / "entry.py").read_bytes()
    assert run_tool("pack", source, "--output", source / "entry.py").returncode == 2
    assert (source / "entry.py").read_bytes() == before


def test_install_scan_failure_preserves_existing_active_plugin(env, tmp_path):
    from tests.plugins.test_runtime import activate, write_plugin

    manager, directory, _ = env
    original = write_plugin(directory)
    record = activate(manager)
    before = (original / "entry.py").read_bytes()
    package = tmp_path / "invalid.zip"
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("plugin.json", (original / "plugin.json").read_bytes())
        archive.writestr("entry.py", "import src.core.config\n")
    with pytest.raises(InstallError):
        manager.install_archive(package)
    assert manager.get(record.id) is record
    assert record.state.value == "active"
    assert (original / "entry.py").read_bytes() == before
