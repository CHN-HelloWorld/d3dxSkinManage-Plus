import ast
import hashlib
import importlib.util
import json
import os
import py_compile
import zipfile
from pathlib import Path

import pytest


@pytest.fixture
def release_source(tmp_path):
    """复制需要写入版本的源码，用独立目录验证发布准备不会修改本地仓库。"""
    root = Path(__file__).resolve().parents[2]
    for relative in ("src/utils/constants.py", "pyproject.toml", "updater/updater.rc", "updater/updater.manifest"):
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((root / relative).read_bytes())
    return tmp_path


def release_inputs(**changes):
    """生成包含中文、Markdown 和 shell 特殊字符的实际发布输入。"""
    return {
        "mode": "publish",
        "version": "v1.2.3",
        "release_title": '版本 1.2.3 · "更新体验"',
        "release_notes": "## 更新内容\\n- 支持中文说明\\n- 保留 `code` 和 $(example)",
        **changes,
    }


def constants_from(path):
    """解析生成源码中的常量，不导入测试工作区之外的应用模块。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.targets[0].id: ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign)
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id in {"APP_VERSION", "APP_UPDATE_REPO"}
    }


def test_prepare_embeds_version_and_repository_in_the_build(release_source):
    from scripts.release import github_release

    metadata = github_release.prepare_release(release_source, release_inputs(), "maintainer/mod-manager")

    assert constants_from(release_source / "src/utils/constants.py") == {
        "APP_VERSION": "1.2.3",
        "APP_UPDATE_REPO": "maintainer/mod-manager",
    }
    assert '\nversion = "1.2.3"\n' in (release_source / "pyproject.toml").read_text(encoding="utf-8")
    resource = (release_source / "updater/updater.rc").read_text(encoding="utf-8")
    assert "FILEVERSION 1,2,3,0" in resource
    assert "PRODUCTVERSION 1,2,3,0" in resource
    assert '"FileVersion", "1.2.3.0\\0"' in resource
    assert '"ProductVersion", "1.2.3.0\\0"' in resource
    manifest = (release_source / "updater/updater.manifest").read_text(encoding="utf-8")
    assert '<assemblyIdentity version="1.2.3.0"' in manifest
    assert metadata["tag"] == "v1.2.3"
    assert metadata["title"] == '版本 1.2.3 · "更新体验"'
    assert metadata["notes"] == "## 更新内容\n- 支持中文说明\n- 保留 `code` 和 $(example)"


def test_same_second_version_change_invalidates_existing_bytecode(release_source, monkeypatch):
    from scripts.release import github_release

    github_release.prepare_release(release_source, release_inputs(version="1.2.3"), "maintainer/mod-manager")
    constants = release_source / "src/utils/constants.py"
    py_compile.compile(str(constants), doraise=True)
    original_stat = constants.stat()
    write_text = Path.write_text

    def write_in_same_second(path, *args, **kwargs):
        """固定常量文件的时间戳，稳定复现等长版本修改命中旧字节码的条件。"""
        result = write_text(path, *args, **kwargs)
        if path == constants:
            os.utime(path, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
        return result

    monkeypatch.setattr(Path, "write_text", write_in_same_second)

    github_release.prepare_release(release_source, release_inputs(version="1.2.4"), "maintainer/mod-manager")

    assert constants.stat().st_size == original_stat.st_size
    assert constants.stat().st_mtime_ns == original_stat.st_mtime_ns
    spec = importlib.util.spec_from_file_location("release_version_fixture", constants)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.APP_VERSION == "1.2.4"


@pytest.mark.parametrize(
    "changes",
    [
        {"version": "1.2.3-beta"},
        {"version": "1.2"},
        {"version": "65536.0.0"},
        {"version": "01.2.3"},
        {"version": "../1.2.3"},
        {"release_title": "   "},
        {"release_notes": "\\n  \\n"},
        {"release_notes": "旧校验值 SHA256: " + "a" * 64},
        {"mode": "unknown"},
    ],
)
def test_invalid_release_inputs_do_not_change_source(release_source, changes):
    from scripts.release import github_release

    before = {path: path.read_bytes() for path in release_source.rglob("*") if path.is_file()}

    with pytest.raises(ValueError):
        github_release.prepare_release(release_source, release_inputs(**changes), "maintainer/mod-manager")

    assert all(path.read_bytes() == contents for path, contents in before.items())
    assert not (release_source / "build/release-inputs.json").exists()


def test_build_only_accepts_empty_release_copy(release_source):
    from scripts.release import github_release

    metadata = github_release.prepare_release(
        release_source,
        release_inputs(mode="build", version="1.2.3.4", release_title="", release_notes=""),
        "maintainer/mod-manager",
    )

    assert metadata["mode"] == "build"
    assert metadata["version"] == "1.2.3.4"
    assert "FILEVERSION 1,2,3,4" in (release_source / "updater/updater.rc").read_text(encoding="utf-8")
    manifest = (release_source / "updater/updater.manifest").read_text(encoding="utf-8")
    assert '<assemblyIdentity version="1.2.3.4"' in manifest


def test_multiline_notes_keep_literal_windows_paths(release_source):
    from scripts.release import github_release

    notes = "## 更新说明\n\n使用目录 `C:\\new\\mods`。\n"
    metadata = github_release.prepare_release(
        release_source, release_inputs(release_notes=notes), "maintainer/mod-manager"
    )

    assert metadata["notes"] == notes


def media_source(root, *, content=b"project video"):
    """创建固定清单及媒体文件，独立计算摘要作为发布资源的信任依据。"""
    directory = root / "resources/about/story"
    directory.mkdir(parents=True)
    manifest = {
        "file": "film.mp4",
        "bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
        "archive": "d3dxSkinManage-Plus-story-media.zip",
        "archive_path": "resources/about/story/film.mp4",
    }
    (directory / "media-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (directory / "film.mp4").write_bytes(content)
    return directory


def test_media_pack_can_restore_the_video_without_extracting_other_members(tmp_path):
    from scripts.release import github_release

    source = tmp_path / "source"
    media_source(source)
    archive = github_release.pack_media(source)
    with zipfile.ZipFile(archive, "a") as package:
        package.writestr("../../outside.txt", b"not a video")

    destination = tmp_path / "destination"
    media_source(destination, content=b"project video")
    (destination / "resources/about/story/film.mp4").write_bytes(b"old media")

    restored = github_release.restore_media(destination, archive)

    assert restored.read_bytes() == b"project video"
    assert not (tmp_path / "outside.txt").exists()
    assert sorted(path.name for path in restored.parent.iterdir()) == ["film.mp4", "media-manifest.json"]


@pytest.mark.parametrize("content", [b"broken video!", b"short"])
def test_wrong_media_never_replaces_the_existing_video(tmp_path, content):
    from scripts.release import github_release

    directory = media_source(tmp_path)
    archive = tmp_path / "download.zip"
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr("resources/about/story/film.mp4", content)

    with pytest.raises(ValueError):
        github_release.restore_media(tmp_path, archive)

    assert (directory / "film.mp4").read_bytes() == b"project video"


def test_bundle_is_readable_by_the_existing_update_checker(release_source, monkeypatch, temp_data_dir):
    from scripts.release import github_release
    from src.services.app_update.checker import AppUpdateChecker

    github_release.prepare_release(release_source, release_inputs(), "maintainer/mod-manager")
    archive = release_source / "build/d3dxSkinManage-Plus-v1.2.3-win64.zip"
    prefix = "d3dxSkinManage-Plus-v1.2.3-win64/"
    with zipfile.ZipFile(archive, "w") as package:
        for name in ("d3dxSkinManage-Plus.exe", "Updater/updater.exe", "files.manifest"):
            package.writestr(prefix + name, b"release content")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()

    bundle = github_release.create_bundle(release_source)
    metadata = json.loads((bundle / "release.json").read_text(encoding="utf-8"))
    body = (bundle / "release-notes.md").read_text(encoding="utf-8")
    release = {
        "tag_name": metadata["tag"],
        "name": metadata["title"],
        "body": body,
        "assets": [
            {
                "name": metadata["asset_name"],
                "size": metadata["asset_size"],
                "browser_download_url": "https://example.test/app.zip",
            }
        ],
    }
    checker = AppUpdateChecker("1.0.0", "maintainer/mod-manager")
    monkeypatch.setattr(checker, "_race_fetch", lambda url, *_: [] if "per_page" in url else release)

    result = checker.check()

    assert result.error is None
    assert result.has_update
    assert result.latest_version == "1.2.3"
    assert result.release_name == '版本 1.2.3 · "更新体验"'
    assert result.changelog.startswith("## 更新内容\n- 支持中文说明\n")
    assert result.sha256 == digest
    assert result.download_url == "https://example.test/app.zip"
    assert (bundle / metadata["asset_name"]).read_bytes() == archive.read_bytes()
    assert (bundle / metadata["checksum_name"]).read_text(encoding="utf-8") == f"{digest}  {archive.name}\n"


def test_bundle_rejects_a_package_missing_the_updater(release_source):
    from scripts.release import github_release

    github_release.prepare_release(release_source, release_inputs(), "maintainer/mod-manager")
    archive = release_source / "build/d3dxSkinManage-Plus-v1.2.3-win64.zip"
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr("d3dxSkinManage-Plus-v1.2.3-win64/d3dxSkinManage-Plus.exe", b"main")

    with pytest.raises(ValueError, match="Updater/updater.exe"):
        github_release.create_bundle(release_source)
