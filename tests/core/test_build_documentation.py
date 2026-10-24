"""验证教程从当前源码生成，并以独立 HTML 纳入两种发行格式。"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile

import pytest

DOCUMENT_NAMES = (
    "d3dxSkinManage-Plus 使用教程.html",
    "d3dxSkinManage-Plus User Guide.html",
    "d3dxSkinManage-Plus 插件开发指南.html",
    "d3dxSkinManage-Plus Plugin Development Guide.html",
)


def _write_files(directory: Path, files: dict[str, str]) -> None:
    """向隔离目录写入明确列出的文档或暂存文件。"""
    for relative, content in files.items():
        target = directory / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


@pytest.fixture
def documentation_environment(tmp_path, monkeypatch):
    """以小型真实生成器替换教程资源，仅替代昂贵的编译和无关资源收集。"""
    from scripts.release import build

    source = tmp_path / "source"
    tutorials = source / "docs/tutorials"
    builder = source / "scripts/tutorials/build.py"
    builder.parent.mkdir(parents=True)
    builder.write_text(
        '"""按输入清单生成独立测试文档。"""\n'
        "import argparse\n"
        "import json\n"
        "from pathlib import Path\n"
        "parser = argparse.ArgumentParser()\n"
        'parser.add_argument("--output-dir", required=True, type=Path)\n'
        "args = parser.parse_args()\n"
        'source = Path(__file__).resolve().parents[2] / "docs/tutorials/source.json"\n'
        'data = json.loads(source.read_text(encoding="utf-8"))\n'
        'for name, content in data["files"].items():\n'
        "    target = args.output_dir / name\n"
        "    target.parent.mkdir(parents=True, exist_ok=True)\n"
        '    target.write_text(content, encoding="utf-8")\n'
        'raise SystemExit(data.get("exit_code", 0))\n',
        encoding="utf-8",
    )
    _write_files(source, {"run_app.py": "", "resources/fixture.txt": "resource", "docs/构建指南.md": "build guide"})
    tutorials.mkdir(parents=True)
    output = tmp_path / "build"
    staging = output / "nuitka"
    compiled = tmp_path / "compiled"
    run_process = subprocess.run

    def run(command, **kwargs):
        """保留教程子进程，使用最小可执行文件模拟 Nuitka 的真实输出结构。"""
        if "nuitka" not in command:
            return run_process(command, **kwargs)
        compiled.write_text("compiled", encoding="utf-8")
        product = staging if "--onefile" in command else staging / "run_app.dist"
        _write_files(
            product,
            {
                build.EXE_NAME: "program",
                "docs/构建指南.md": "old build guide",
                "docs/old-draft.html": "old draft",
                "docs/tools/build.py": "source builder",
            },
        )
        return subprocess.CompletedProcess(command, 0)

    def empty_directory(relative):
        """为无关发行资源提供空目录，避免读取大型媒体和环境许可。"""

        def stage(directory):
            """返回对应的发行目录。"""
            path = directory / relative
            path.mkdir(parents=True, exist_ok=True)
            return path

        return stage

    monkeypatch.setattr(build, "ROOT", source)
    monkeypatch.setattr(build, "ENTRY", source / "run_app.py")
    monkeypatch.setattr(build, "RESOURCES", source / "resources")
    monkeypatch.setattr(build, "PROJECT_BUILD", output)
    monkeypatch.setattr(build, "STAGING_ROOT", output)
    monkeypatch.setattr(build, "STAGING_DIR", staging)
    monkeypatch.setattr(build, "OUTPUT_DIR", staging)
    monkeypatch.setattr(build, "check_env", lambda: True)
    monkeypatch.setattr(build.subprocess, "run", run)
    monkeypatch.setattr(build, "stage_language_packs", empty_directory("config/languages"))
    monkeypatch.setattr(build, "stage_plugins", empty_directory("plugins"))
    monkeypatch.setattr(build, "stage_project_story", empty_directory("resources/about/story"))
    monkeypatch.setattr(build, "stage_legal_notices", lambda directory: ())
    return SimpleNamespace(build=build, source=source, tutorials=tutorials, output=output, compiled=compiled)


def _set_tutorial_source(environment, files: dict[str, str], *, exit_code: int = 0) -> None:
    """更新生成器输入，模拟当前教程源码变化或构建失败。"""
    (environment.tutorials / "source.json").write_text(
        json.dumps({"files": files, "exit_code": exit_code}, ensure_ascii=False), encoding="utf-8"
    )


@pytest.mark.parametrize("onefile", [False, True])
def test_build_distributes_current_tutorials_in_product_manifest_and_zip(
    documentation_environment, monkeypatch, onefile
):
    env = documentation_environment
    build = env.build
    product = env.output if onefile else env.output / build.DIST_NAME
    monkeypatch.setattr(sys, "argv", ["build.py", "--zip", *(["--onefile"] if onefile else [])])

    for revision in ("first", "updated"):
        documents = {name: f"<!doctype html><p>{revision}: {name}</p>" for name in DOCUMENT_NAMES}
        _set_tutorial_source(env, {**documents, "构建指南.md": "build guide", "draft.html": "draft"})

        assert build.main() == 0

        assert (product / "docs").is_dir()
        assert {path.name for path in (product / "docs").iterdir()} == set(DOCUMENT_NAMES)
        for name, content in documents.items():
            assert (product / "docs" / name).read_text(encoding="utf-8") == content
        manifest = (product / "files.manifest").read_text(encoding="utf-8")
        managed, preserve = manifest.split("[preserve]")
        assert set(managed.splitlines()) - {"", "[managed]"} == {
            build.EXE_NAME,
            *(f"docs/{name}" for name in DOCUMENT_NAMES),
        }
        assert "docs/" not in preserve.splitlines()
        with ZipFile(next(env.output.glob("*.zip"))) as archive:
            entries = {name.split("/", 1)[1]: name for name in archive.namelist()}
            assert set(entries) == {build.EXE_NAME, "files.manifest", *(f"docs/{name}" for name in DOCUMENT_NAMES)}
            for name, content in documents.items():
                assert archive.read(entries[f"docs/{name}"]).decode("utf-8") == content


@pytest.mark.parametrize("failure", ["exit_code", "missing_file"])
def test_tutorial_generation_failure_stops_before_compilation(documentation_environment, monkeypatch, failure):
    env = documentation_environment
    documents = {name: "<!doctype html><p>old output</p>" for name in DOCUMENT_NAMES}
    _write_files(env.output / "tutorials", documents)
    current = documents if failure == "exit_code" else {name: documents[name] for name in DOCUMENT_NAMES[:-1]}
    _set_tutorial_source(env, current, exit_code=7 if failure == "exit_code" else 0)
    monkeypatch.setattr(sys, "argv", ["build.py", "--zip"])

    assert env.build.main() != 0

    assert not env.compiled.exists()
    assert not list(env.output.glob("*.zip"))


@pytest.mark.parametrize("docs_directory", ["docs", "DOCS"])
def test_dirty_documentation_is_excluded_from_release_zip_and_manifest(tmp_path, monkeypatch, docs_directory):
    from scripts.release import build

    product = tmp_path / "product"
    documents = {f"docs/{name}": "<!doctype html><p>release tutorial</p>" for name in DOCUMENT_NAMES}
    excluded = {
        f"{docs_directory}/构建指南.md": "developer instructions",
        f"{docs_directory}/用户指南.md": "old guide",
        f"{docs_directory}/插件开发指南.md": "old plugin guide",
        f"{docs_directory}/tools/build.py": "source tool",
        f"{docs_directory}/draft.html": "draft",
        f"{docs_directory}/local.txt": "local note",
    }
    _write_files(product, {build.EXE_NAME: "program", **excluded, **documents})
    monkeypatch.setattr(build, "PROJECT_BUILD", tmp_path)

    manifest = build.generate_release_manifest([(product, "")], product)
    archive = build.make_zip([(product, "")])

    manifest_paths = {line.casefold() for line in manifest.read_text(encoding="utf-8").splitlines()}
    assert {path.casefold() for path in documents} <= manifest_paths
    assert not {path.casefold() for path in excluded} & manifest_paths
    with ZipFile(archive) as package:
        names = {name.split("/", 1)[1].casefold() for name in package.namelist()}
        assert names == {path.casefold() for path in (build.EXE_NAME, "files.manifest", *documents)}


def test_clean_removes_generated_documentation_but_preserves_sources(documentation_environment):
    env = documentation_environment
    _write_files(env.output / "tutorials", {name: "generated" for name in DOCUMENT_NAMES})
    _write_files(env.output / "docs", {name: "shipped" for name in DOCUMENT_NAMES})
    _write_files(env.output, {"unrelated.txt": "retained"})

    assert env.build.clean_outputs() == 0

    assert not (env.output / "tutorials").exists()
    assert not (env.output / "docs").exists()
    assert (env.source / "docs/构建指南.md").read_text(encoding="utf-8") == "build guide"
    assert (env.source / "scripts/tutorials/build.py").is_file()
    assert (env.output / "unrelated.txt").read_text(encoding="utf-8") == "retained"
