"""在独立源码目录验证本地与云端发行入口，仅替代耗时的编译进程。"""

import ast
import hashlib
import json
import os
import shutil
import subprocess
import sys
import textwrap
import zipfile
from pathlib import Path

import pytest

VERSION_FILES = ("src/utils/constants.py", "pyproject.toml", "updater/updater.rc", "updater/updater.manifest")


@pytest.fixture
def release_project(tmp_path):
    """复制实际发行工具，用子进程读取同步后的源码并模拟编译产物。"""
    root = Path(__file__).resolve().parents[2]
    project = tmp_path / "中文 release project"
    for relative in VERSION_FILES:
        target = project / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / relative, target)
    scripts = project / "scripts/release"
    scripts.mkdir(parents=True)
    for source in (root / "scripts/release").glob("*.py"):
        shutil.copy2(source, scripts / source.name)
    updater = project / "updater/tools/build.py"
    updater.parent.mkdir(parents=True)
    updater.write_text(
        textwrap.dedent("""\
            import os
            import sys
            from pathlib import Path

            root = Path(__file__).resolve().parents[2]
            assert sys.argv[1:] == ["--updater-only"], sys.argv
            (root / "updater-started").touch()
            sys.exit(int(os.environ.get("D3DX_TEST_UPDATER_EXIT", "0")))
        """),
        encoding="utf-8",
    )
    (scripts / "build.py").write_text(
        textwrap.dedent("""\
            import ast
            import json
            import os
            import sys
            import zipfile
            from pathlib import Path

            root = Path(__file__).resolve().parents[2]
            (root / "main-started").touch()
            if "--check" in sys.argv:
                sys.exit(0)
            assert (root / "updater-started").is_file()
            code = int(os.environ.get("D3DX_TEST_MAIN_EXIT", "0"))
            if code:
                sys.exit(code)
            tree = ast.parse((root / "src/utils/constants.py").read_text(encoding="utf-8"))
            version = next(
                ast.literal_eval(node.value) for node in tree.body
                if isinstance(node, ast.Assign) and node.targets[0].id == "APP_VERSION"
            )
            build = root / "build"
            build.mkdir(exist_ok=True)
            (build / "compiler-arguments.json").write_text(json.dumps(sys.argv[1:]), encoding="utf-8")
            stem = f"d3dxSkinManage-Plus-v{version}-win64"
            with zipfile.ZipFile(build / f"{stem}.zip", "w") as package:
                for name in ("d3dxSkinManage-Plus.exe", "Updater/updater.exe", "files.manifest"):
                    package.writestr(f"{stem}/{name}", version.encode())
        """),
        encoding="utf-8",
    )
    return project


def run_release(project, *arguments, input_text=None, environment=None):
    """从源码目录外运行真实入口，避免依赖调用者的工作目录及控制台编码。"""
    return subprocess.run(
        [sys.executable, "-X", "utf8", str(project / "scripts/release/release.py"), *arguments],
        cwd=project.parent,
        env={**os.environ, **(environment or {})},
        input=input_text,
        capture_output=True,
        encoding="utf-8",
        timeout=20,
    )


def read_version(project):
    """读取入口实际写入的版本常量，避免导入其他工作区中已缓存的模块。"""
    tree = ast.parse((project / "src/utils/constants.py").read_text(encoding="utf-8"))
    return next(
        ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign) and node.targets[0].id == "APP_VERSION"
    )


def test_local_release_builds_the_requested_version_and_checksum(release_project):
    notes = release_project / "中文 notes.md"
    notes.write_text("## 更新内容\n\n保留路径 `C:\\new\\mods` 和中文说明。\n", encoding="utf-8")

    result = run_release(
        release_project,
        "--version",
        "v1.2.3",
        "--title",
        "v1.2.3 · 功能改进",
        "--notes-file",
        str(notes),
        "--",
        "--msvc=latest",
        "--jobs=2",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    bundle = release_project / "build/release"
    archive = bundle / "d3dxSkinManage-Plus-v1.2.3-win64.zip"
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    assert read_version(release_project) == "1.2.3"
    assert 'version = "1.2.3"' in (release_project / "pyproject.toml").read_text(encoding="utf-8")
    assert "FILEVERSION 1,2,3,0" in (release_project / "updater/updater.rc").read_text(encoding="utf-8")
    assert 'version="1.2.3.0"' in (release_project / "updater/updater.manifest").read_text(encoding="utf-8")
    with zipfile.ZipFile(archive) as package:
        assert package.read("d3dxSkinManage-Plus-v1.2.3-win64/d3dxSkinManage-Plus.exe") == b"1.2.3"
    assert (bundle / (archive.name + ".sha256")).read_text(encoding="utf-8") == f"{digest}  {archive.name}\n"
    assert (bundle / "release-notes.md").read_text(encoding="utf-8").startswith(notes.read_text(encoding="utf-8"))
    assert json.loads((release_project / "build/compiler-arguments.json").read_text(encoding="utf-8")) == [
        "--with-updater",
        "--zip",
        "--",
        "--msvc=latest",
        "--jobs=2",
    ]


def test_workflow_prepare_and_build_share_version_and_release_copy(release_project):
    event = release_project / "event.json"
    event.write_text(
        json.dumps(
            {
                "inputs": {
                    "mode": "publish",
                    "version": "v2.3.4",
                    "release_title": "v2.3.4 · 更新",
                    "release_notes": "## 更新内容\\n- 中文和 `code`、$(example)",
                }
            }
        ),
        encoding="utf-8",
    )
    output = release_project / "github-output"
    environment = {
        "GITHUB_EVENT_PATH": str(event),
        "GITHUB_REPOSITORY": "maintainer/mod-manager",
        "GITHUB_OUTPUT": str(output),
    }

    prepared = run_release(release_project, "--from-workflow", "--prepare-only", environment=environment)

    assert prepared.returncode == 0, prepared.stdout + prepared.stderr
    assert not (release_project / "updater-started").exists()
    assert not (release_project / "main-started").exists()
    assert "tag=v2.3.4\n" in output.read_text(encoding="utf-8")
    result = run_release(release_project, "--from-workflow", environment=environment)

    assert result.returncode == 0, result.stdout + result.stderr
    metadata = json.loads((release_project / "build/release/release.json").read_text(encoding="utf-8"))
    assert metadata["version"] == "2.3.4"
    assert metadata["repository"] == "maintainer/mod-manager"
    assert metadata["title"] == "v2.3.4 · 更新"
    assert metadata["notes"] == "## 更新内容\n- 中文和 `code`、$(example)"
    assert metadata["mode"] == "publish"


def test_interactive_version_change_retries_invalid_input_without_building(release_project):
    result = run_release(release_project, "--interactive", "--prepare-only", input_text="bad\nv1.4.2\n")

    assert result.returncode == 0, result.stdout + result.stderr
    assert read_version(release_project) == "1.4.2"
    assert not (release_project / "updater-started").exists()
    assert not (release_project / "main-started").exists()


@pytest.mark.parametrize("arguments", [("--version", "1.2.3-beta"), ("--notes-file", "missing-notes.md")])
def test_invalid_local_inputs_leave_source_unchanged(release_project, arguments):
    before = {relative: (release_project / relative).read_bytes() for relative in VERSION_FILES}

    result = run_release(release_project, *arguments)

    assert result.returncode != 0
    assert all((release_project / relative).read_bytes() == contents for relative, contents in before.items())
    assert not (release_project / "updater-started").exists()
    assert not (release_project / "build").exists()


def test_dry_run_does_not_change_versions_or_create_outputs(release_project):
    before = {relative: (release_project / relative).read_bytes() for relative in VERSION_FILES}

    result = run_release(release_project, "--version", "1.8.9", "--dry-run")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "1.8.9" in result.stdout
    assert "--updater-only" in result.stdout
    assert all((release_project / relative).read_bytes() == contents for relative, contents in before.items())
    assert not (release_project / "updater-started").exists()
    assert not (release_project / "build").exists()


@pytest.mark.parametrize("stage,code", [("UPDATER", 23), ("MAIN", 25)])
def test_failed_compilation_stops_before_bundle_creation(release_project, stage, code):
    result = run_release(release_project, "--version", "1.2.3", environment={f"D3DX_TEST_{stage}_EXIT": str(code)})

    assert result.returncode == code, result.stdout + result.stderr
    assert not (release_project / "build/release").exists()
    if stage == "UPDATER":
        assert not (release_project / "main-started").exists()
