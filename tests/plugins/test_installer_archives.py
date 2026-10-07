from __future__ import annotations

import json
import subprocess
import zipfile

import pytest

from src.plugins.runtime import installer
from src.services import archive as archive_service


def _package(tmp_path, kind, *, wrapped=False, version="1.0.0", missing_entry=False):
    members = {
        "plugin.json": json.dumps(
            {
                "id": "test.archive",
                "name": "Archive plugin",
                "version": version,
                "apiVersion": "2.0",
                "entry": "entry.py",
            }
        ).encode(),
        "entry.py": b"raise RuntimeError('installation must not execute plugin code')\n",
        "assets/说明.txt": "插件资源".encode(),
    }
    if missing_entry:
        del members["entry.py"]
    if wrapped:
        members = {"plugin/" + name: data for name, data in members.items()}
        members["README.txt"] = b"Package readme outside the plugin root"
    package = tmp_path / f"package-{version}.{kind}"
    if kind == "zip":
        with zipfile.ZipFile(package, "w") as output:
            for name, data in members.items():
                output.writestr(name, data)
    elif kind == "7z":
        py7zr = pytest.importorskip("py7zr")
        with py7zr.SevenZipFile(package, "w") as output:
            for name, data in members.items():
                output.writestr(data, name)
    else:
        rar_exe = archive_service.find_winrar_rar_exe()
        if not rar_exe:
            pytest.skip("Creating a real RAR fixture requires WinRAR")
        source = tmp_path / f"source-{version}"
        for name, data in members.items():
            destination = source / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
        subprocess.run(
            [rar_exe, "a", "-idq", "-r", str(package), "*"],
            cwd=source,
            check=True,
            capture_output=True,
            timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    return package


@pytest.mark.parametrize("kind", ["zip", "7z", "rar"])
@pytest.mark.parametrize("wrapped", [False, True])
def test_installs_archive_layout_and_resources(tmp_path, kind, wrapped):
    package = _package(tmp_path, kind, wrapped=wrapped)
    plugins = tmp_path / "plugins"

    result = installer.install_from_archive(package, plugins_dir=plugins)

    assert result.plugin_id == "test.archive"
    assert result.version == "1.0.0" and not result.was_update
    assert result.directory == plugins / "test.archive"
    assert (result.directory / "assets/说明.txt").read_text("utf-8") == "插件资源"
    assert (result.directory / "entry.py").is_file()
    assert not (result.directory / "README.txt").exists()
    assert [path.name for path in plugins.iterdir()] == ["test.archive"]


@pytest.mark.parametrize("kind", ["7z", "rar"])
def test_archive_update_validates_before_replacing_installed_files(tmp_path, kind):
    plugins = tmp_path / "plugins"
    initial = _package(tmp_path, "zip")
    installed = installer.install_from_archive(initial, plugins_dir=plugins)
    (installed.directory / "old-only.txt").write_text("old version", encoding="utf-8")
    invalid = _package(tmp_path, kind, version="2.0.0", missing_entry=True)

    with pytest.raises(installer.InstallError):
        installer.install_from_archive(invalid, plugins_dir=plugins)
    assert (installed.directory / "old-only.txt").read_text("utf-8") == "old version"
    assert json.loads((installed.directory / "plugin.json").read_text("utf-8"))["version"] == "1.0.0"

    update = _package(tmp_path, kind, version="3.0.0", wrapped=True)
    result = installer.install_from_archive(update, plugins_dir=plugins)

    assert result.was_update and result.previous_version == "1.0.0"
    assert result.version == "3.0.0"
    assert not (result.directory / "old-only.txt").exists()
    assert (result.directory / "assets/说明.txt").read_text("utf-8") == "插件资源"
    assert [path.name for path in plugins.iterdir()] == ["test.archive"]
