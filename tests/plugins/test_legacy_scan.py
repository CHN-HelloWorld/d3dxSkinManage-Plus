"""用隔离旧版目录验证只读迁移扫描、规则匹配和资源边界。"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import sys
import zipfile
from pathlib import Path
from types import ModuleType

import pytest


def _reader():
    """直接加载独立解析模块，避免扫描测试依赖插件界面与宿主。"""
    path = Path(__file__).resolve().parents[2] / "plugins/official.legacy-migration/legacy.py"
    assert path.is_file(), "旧版只读解析器尚未实现"
    package_name = "_legacy_scan_test_package"
    module_name = package_name + ".legacy"
    if module_name not in sys.modules:
        package = ModuleType(package_name)
        package.__path__ = [str(path.parent)]
        sys.modules[package_name] = package
        spec = importlib.util.spec_from_file_location(module_name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
    return sys.modules[module_name]


def _layout(tmp_path: Path) -> tuple[Path, Path]:
    """构造不包含真实配置的旧版安装目录与默认用户环境。"""
    root = tmp_path / "legacy"
    profile = root / "home/primary"
    for path in (profile / "modsIndex", profile / "classification", root / "resources/mods"):
        path.mkdir(parents=True)
    return root, profile


def _json(path: Path, value: object) -> None:
    """以旧版 UTF-8 JSON 格式写入合成配置。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8", newline="\n")


def _entry(**values) -> dict:
    """提供完整的中性旧版条目，按用例覆盖需要验证的字段。"""
    return {
        "object": "Unit A",
        "type": "zip",
        "name": "Sample Mod",
        "author": "Sample Author",
        "explain": "Sample description",
        "grading": "R",
        "tags": ["blue", "alternate"],
        **values,
    }


def _archive(root: Path, content: str = "sample") -> tuple[str, Path]:
    """按原始 ZIP 字节摘要命名无扩展名资源。"""
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as package:
        package.writestr("unit.ini", content)
    data = stream.getvalue()
    sha = hashlib.sha1(data).hexdigest()
    path = root / "resources/mods" / sha
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return sha, path


def _image(path: Path) -> Path:
    """提供用于定位测试的图片文件；图像解码由宿主导入测试负责。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"local image bytes:" + path.name.encode("utf-8"))
    return path


def _scan(reader, root: Path, **kwargs):
    """通过公开发现接口取得环境，再调用公开扫描入口。"""
    return reader.scan_environment(reader.discover_environments(str(root))[0], **kwargs)


def _snapshot(root: Path) -> dict:
    """记录文件内容、修改时间和目录集合，验证扫描不会写入旧库。"""
    return {
        str(path.relative_to(root)): (path.read_bytes(), path.stat().st_mtime_ns) if path.is_file() else None
        for path in root.rglob("*")
    }


def _symlink(path: Path, target: Path, *, directory: bool = False) -> None:
    """创建真实链接；无符号链接权限的平台明确跳过相应用例。"""
    try:
        path.symlink_to(target, target_is_directory=directory)
    except OSError as error:
        pytest.skip(f"当前平台不能创建测试链接: {error}")


@pytest.mark.parametrize("disabled", [False, True])
def test_readable_index_key_resolves_archive_cache_and_previews_with_real_sha(tmp_path, disabled):
    reader = _reader()
    root, profile = _layout(tmp_path)
    sha, original = _archive(root)
    key = "角色甲--作者-BuildA"
    archive = original.with_name(key)
    original.rename(archive)
    index_name = "disabled-collection.json" if disabled else "collection.json"
    _json(profile / "modsIndex" / index_name, {"mods": {key: _entry()}})
    cache = profile / "work/Mods" / ("disabled-" + key)
    cache.mkdir(parents=True)
    (cache / "unit.ini").write_text("sample", encoding="utf-8")
    preview = _image(root / "resources/preview" / (key + ".png"))
    before = _snapshot(root)

    result = _scan(reader, root)

    assert len(result.mods) == 1
    mod = result.mods[0]
    assert mod.legacy_key == key
    assert mod.sha == sha
    assert mod.archive_path == str(archive)
    assert mod.cache_path == str(cache)
    assert not mod.cache_changed
    assert mod.preview_paths == (str(preview),)
    assert before == _snapshot(root)


def test_readable_cache_only_key_does_not_invent_an_archive_sha(tmp_path):
    reader = _reader()
    root, profile = _layout(tmp_path)
    key = "角色甲--自定义缓存"
    _json(profile / "modsIndex/collection.json", {"mods": {key: _entry()}})
    cache = profile / "work/Mods" / key
    cache.mkdir(parents=True)
    (cache / "unit.ini").write_text("edited", encoding="utf-8")

    result = _scan(reader, root)

    assert len(result.mods) == 1
    mod = result.mods[0]
    assert mod.legacy_key == key
    assert mod.sha == ""
    assert mod.source_path == str(cache)
    assert len(mod.cache_digest) == 64
    assert any("缓存可能不完整" in issue.message for issue in result.issues)


def test_unicode_casefold_distinct_named_archives_remain_independent(tmp_path):
    reader = _reader()
    root, profile = _layout(tmp_path)
    expected = {}
    for key in ("Stra\u00dfe", "STRASSE"):
        sha, original = _archive(root, key)
        archive = original.with_name(key)
        original.rename(archive)
        expected[key] = (sha, str(archive))
    _json(profile / "modsIndex/main.json", {"mods": {key: _entry(name=key) for key in expected}})
    before = _snapshot(root)

    result = _scan(reader, root)

    assert len(result.mods) == 2
    assert {mod.legacy_key: (mod.sha, mod.archive_path) for mod in result.mods} == expected
    assert {mod.name for mod in result.mods} == set(expected)
    assert not result.issues
    assert before == _snapshot(root)


@pytest.mark.parametrize("key", ["Stra\u00dfe", "STRASSE"])
@pytest.mark.parametrize("prefix", ["", "disabled-"])
@pytest.mark.parametrize("matching_cache_exists", [False, True])
def test_unicode_casefold_distinct_named_cache_lookup_does_not_mix_sources(
    tmp_path, key, prefix, matching_cache_exists
):
    reader = _reader()
    root, profile = _layout(tmp_path)
    other_key = "STRASSE" if key == "Stra\u00dfe" else "Stra\u00dfe"
    other_cache = profile / "work/Mods" / (prefix + other_key)
    other_cache.mkdir(parents=True)
    (other_cache / "unit.ini").write_text("unrelated content", encoding="utf-8")
    matching_cache = profile / "work/Mods" / (prefix + key)
    if matching_cache_exists:
        matching_cache.mkdir()
        (matching_cache / "unit.ini").write_text("selected content", encoding="utf-8")
    _json(profile / "modsIndex/main.json", {"mods": {key: _entry(name=key)}})
    before = _snapshot(root)

    result = _scan(reader, root)

    if matching_cache_exists:
        assert len(result.mods) == 1
        mod = result.mods[0]
        assert mod.legacy_key == key
        assert mod.sha == ""
        assert mod.source_path == mod.cache_path == str(matching_cache)
        assert mod.cache_digest == reader.directory_fingerprint(str(matching_cache))
        assert not any("????????" in issue.message for issue in result.issues)
    else:
        assert not result.mods
        assert any(issue.code == "missing" for issue in result.issues)
    assert before == _snapshot(root)


def test_ascii_sha_index_aliases_keep_normalization_and_active_precedence(tmp_path):
    reader = _reader()
    root, profile = _layout(tmp_path)
    sha, archive = _archive(root)
    _json(profile / "modsIndex/active.json", {"mods": {sha.upper(): _entry(name="Active")}})
    _json(profile / "modsIndex/disabled-main.json", {"mods": {sha: _entry(name="Disabled")}})
    cache = profile / "work/Mods" / ("disabled-" + sha)
    cache.mkdir(parents=True)
    (cache / "unit.ini").write_text("sample", encoding="utf-8")
    before = _snapshot(root)

    result = _scan(reader, root)

    assert len(result.mods) == 1
    mod = result.mods[0]
    assert mod.sha == mod.legacy_key == sha
    assert mod.name == "Active"
    assert mod.archive_path == str(archive)
    assert mod.cache_path == str(cache)
    assert not mod.cache_changed
    assert before == _snapshot(root)


def test_distinct_named_keys_preserve_cancellation_between_mods(tmp_path):
    reader = _reader()
    root, profile = _layout(tmp_path)
    mods = {}
    for key in ("Stra\u00dfe", "STRASSE"):
        _, original = _archive(root, key)
        original.rename(original.with_name(key))
        mods[key] = _entry(name=key)
    _json(profile / "modsIndex/main.json", {"mods": mods})
    before = _snapshot(root)
    updates = []

    def progress(completed, total, message):
        """记录逐项扫描进度，在首项完成后触发取消检查。"""
        updates.append((completed, total, message))

    with pytest.raises(InterruptedError):
        _scan(reader, root, progress=progress, should_cancel=lambda: any(row[0] == 1 for row in updates))

    assert updates[-1][0:2] == (1, 2)
    assert before == _snapshot(root)


def test_readable_archive_key_uses_new_content_identity_after_replacement(tmp_path):
    reader = _reader()
    root, profile = _layout(tmp_path)
    sha, original = _archive(root, "original")
    key = "角色甲--自定义原包"
    archive = original.with_name(key)
    original.rename(archive)
    _json(profile / "modsIndex/collection.json", {"mods": {key: _entry()}})
    first = _scan(reader, root)
    changed_sha, replacement = _archive(root, "changed")
    archive.write_bytes(replacement.read_bytes())
    replacement.unlink()
    second = _scan(reader, root)

    assert len(first.mods) == len(second.mods) == 1
    assert first.mods[0].sha == sha
    assert second.mods[0].sha == changed_sha != sha
    assert first.mods[0].legacy_key == second.mods[0].legacy_key == key


@pytest.mark.parametrize(
    "key", ["../escape", "..\\escape", "C:\\escape", "name:stream", "CON", "NUL.zip", "name.", "name ", "bad\x00key"]
)
def test_readable_index_keys_cannot_escape_or_alias_windows_paths(tmp_path, key):
    reader = _reader()
    root, profile = _layout(tmp_path)
    _json(profile / "modsIndex/collection.json", {"mods": {key: _entry()}})

    result = _scan(reader, root)

    assert not result.mods
    assert any(issue.code == "invalid" and issue.name == key for issue in result.issues)


def test_discover_uses_direct_home_directories_and_preserves_environment_names(tmp_path):
    reader = _reader()
    root, profile = _layout(tmp_path)
    (root / "home/secondary").mkdir()
    (root / "home/notes.txt").write_text("notes", encoding="utf-8")
    environments = reader.discover_environments(str(root))
    assert [(env.key, env.name) for env in environments] == [("primary", "primary"), ("secondary", "secondary")]
    assert environments[0].path == str(profile)
    assert {env.root for env in environments} == {str(root)}


def test_discover_rejects_a_directory_without_old_home(tmp_path):
    reader = _reader()
    with pytest.raises(ValueError):
        reader.discover_environments(str(tmp_path))


def test_scan_preserves_metadata_shared_resources_and_literal_category_names(tmp_path):
    reader = _reader()
    root, profile = _layout(tmp_path)
    sha, archive = _archive(root)
    _json(
        profile / "modsIndex/nested/mods.JSON",
        {
            "variable": {"base": "https://example.invalid"},
            "mods": {
                sha: _entry(get=[{"url": "($.base)/sample.zip", "mode": "get"}]),
            },
        },
    )
    (profile / "classification/Collection.Primary").write_text("unit ?\n", encoding="utf-8")
    preview = _image(root / "resources/preview" / f"{sha}.png")
    fullscreen = _image(root / "resources/preview_screen" / f"{sha}.jpg")
    thumbnail = _image(profile / "thumbnail/Unit A.png")
    before = _snapshot(root)

    result = _scan(reader, root)

    assert result.issues == ()
    assert len(result.mods) == 1
    mod = result.mods[0]
    assert (mod.sha, mod.source_path, mod.name) == (sha, str(archive), "Sample Mod")
    assert (mod.author, mod.description, mod.rating, mod.tags) == (
        "Sample Author",
        "Sample description",
        "R",
        ("blue", "alternate"),
    )
    assert mod.object_name == "Unit A"
    assert mod.category_path == ("Collection.Primary", "Unit A")
    assert mod.preview_paths == (str(preview), str(fullscreen))
    assert mod.source_url == "https://example.invalid/sample.zip"
    assert mod.size_bytes == archive.stat().st_size == result.total_bytes
    assert [(item.path, item.node_type, item.thumbnail_path) for item in result.categories] == [
        (("Collection.Primary",), "category", ""),
        (("Collection.Primary", "Unit A"), "character", str(thumbnail)),
    ]
    assert _snapshot(root) == before


def test_active_index_takes_precedence_over_disabled_conflicting_metadata(tmp_path):
    reader = _reader()
    root, profile = _layout(tmp_path)
    sha, _ = _archive(root)
    _json(profile / "modsIndex/active.json", {"mods": {sha: _entry()}})
    _json(profile / "modsIndex/DISABLED-extra.json", {"mods": {sha: _entry(name="Conflicting")}})
    _json(profile / "modsIndex/nested/disabled-folder/hidden.json", {"mods": {sha: _entry(name="Conflicting")}})
    result = _scan(reader, root)
    assert [mod.name for mod in result.mods] == ["Sample Mod"]
    assert result.mods[0].category_path == ("未分类", "Unit A")
    assert any("停用索引已跳过" in issue.message for issue in result.issues)


def test_missing_archive_restores_cache_without_downloading(tmp_path):
    reader = _reader()
    root, profile = _layout(tmp_path)
    sha = "a" * 40
    _json(
        profile / "modsIndex/main.json",
        {
            "mods": {
                sha: _entry(
                    get=[
                        {
                            "url": "https://example.invalid/missing.zip",
                            "mode": "get",
                        }
                    ]
                )
            }
        },
    )
    (profile / "work/Mods" / sha).mkdir(parents=True)
    (profile / "work/Mods" / sha / "unit.ini").write_text("local", encoding="utf-8")
    before = _snapshot(root)
    result = _scan(reader, root)
    assert len(result.mods) == 1
    assert result.total_bytes == len("local")
    assert any("原包缺失" in issue.message and issue.name == "Sample Mod" for issue in result.issues)
    assert _snapshot(root) == before


@pytest.mark.parametrize("payload", [[], {"mods": []}, {"mods": {"../outside": _entry()}}, {"mods": {"a" * 40: []}}])
def test_invalid_index_shapes_and_sha_paths_are_reported(tmp_path, payload):
    reader = _reader()
    root, profile = _layout(tmp_path)
    _json(profile / "modsIndex/main.json", payload)
    result = _scan(reader, root)
    assert result.mods == ()
    assert any(issue.code == "invalid" for issue in result.issues)


def test_invalid_json_does_not_prevent_scanning_other_index_files(tmp_path):
    reader = _reader()
    root, profile = _layout(tmp_path)
    sha, _ = _archive(root)
    _json(profile / "modsIndex/valid.json", {"mods": {sha: _entry()}})
    (profile / "modsIndex/broken.json").write_text("{", encoding="utf-8")
    result = _scan(reader, root)
    assert len(result.mods) == 1
    assert any(issue.code == "invalid" and issue.source.endswith("broken.json") for issue in result.issues)


def test_duplicate_json_keys_are_reported_instead_of_overwritten(tmp_path):
    reader = _reader()
    root, profile = _layout(tmp_path)
    sha, _ = _archive(root)
    item = json.dumps(_entry())
    (profile / "modsIndex/main.json").write_text(f'{{"mods":{{"{sha}":{item},"{sha}":{item}}}}}', encoding="utf-8")
    result = _scan(reader, root)
    assert result.mods == ()
    assert any(issue.code == "invalid" for issue in result.issues)


@pytest.mark.parametrize("field,value", [("object", ""), ("name", []), ("grading", "unknown"), ("tags", [7])])
def test_invalid_metadata_is_not_silently_changed_to_defaults(tmp_path, field, value):
    reader = _reader()
    root, profile = _layout(tmp_path)
    sha, _ = _archive(root)
    _json(profile / "modsIndex/main.json", {"mods": {sha: _entry(**{field: value})}})
    result = _scan(reader, root)
    assert result.mods == ()
    assert any(issue.code == "invalid" for issue in result.issues)


def test_identical_sha_records_are_deduplicated_with_a_warning(tmp_path):
    reader = _reader()
    root, profile = _layout(tmp_path)
    sha, archive = _archive(root)
    for name in ("first", "second"):
        _json(profile / f"modsIndex/{name}.json", {"mods": {sha: _entry()}})
    result = _scan(reader, root)
    assert len(result.mods) == 1
    assert result.total_bytes == archive.stat().st_size
    assert any(issue.code == "warning" for issue in result.issues)


def test_conflicting_sha_records_are_reported_without_choosing_one(tmp_path):
    reader = _reader()
    root, profile = _layout(tmp_path)
    sha, _ = _archive(root)
    _json(profile / "modsIndex/first.json", {"mods": {sha: _entry()}})
    _json(profile / "modsIndex/second.json", {"mods": {sha: _entry(author="Other Author")}})
    result = _scan(reader, root)
    assert result.mods == ()
    assert any(issue.code == "invalid" and issue.name == sha for issue in result.issues)


def test_overlapping_category_rules_do_not_choose_an_arbitrary_destination(tmp_path):
    reader = _reader()
    root, profile = _layout(tmp_path)
    sha, _ = _archive(root)
    _json(profile / "modsIndex/main.json", {"mods": {sha: _entry()}})
    (profile / "classification/First").write_text("Unit *\n", encoding="utf-8")
    (profile / "classification/Second").write_text("Unit A\n", encoding="utf-8")
    result = _scan(reader, root)
    assert result.mods == ()
    assert any(issue.code == "invalid" and issue.name == "Unit A" for issue in result.issues)


def test_thumbnail_aliases_and_directory_rules_follow_configuration_order(tmp_path):
    reader = _reader()
    root, profile = _layout(tmp_path)
    sha, _ = _archive(root)
    _json(profile / "modsIndex/main.json", {"mods": {sha: _entry()}})
    _image(profile / "thumbnail/Unit A.png")
    _image(profile / "thumbnail/pack/Unit A.jpg")
    expected = _image(profile / "thumbnail/pack/selected.png")
    (profile / "thumbnail/_redirection.ini").write_text(
        "; local rules\n[*] pack\\*\nUnit A = pack\\selected.png\n",
        encoding="utf-8",
    )
    result = _scan(reader, root)
    leaf = next(category for category in result.categories if category.node_type == "character")
    assert leaf.thumbnail_path == str(expected)
    assert result.issues == ()


def test_thumbnail_redirection_outside_selected_root_is_rejected(tmp_path):
    reader = _reader()
    root, profile = _layout(tmp_path)
    sha, _ = _archive(root)
    _json(profile / "modsIndex/main.json", {"mods": {sha: _entry()}})
    outside = _image(tmp_path / "outside.png")
    (profile / "thumbnail").mkdir()
    (profile / "thumbnail/_redirection.ini").write_text(f"Unit A = {outside}\n", encoding="utf-8")
    result = _scan(reader, root)
    assert len(result.mods) == 1
    assert not any(category.thumbnail_path for category in result.categories)
    assert any(issue.code == "invalid" and issue.source == str(outside) for issue in result.issues)


def test_approximate_thumbnails_and_unknown_directives_remain_visible(tmp_path):
    reader = _reader()
    root, profile = _layout(tmp_path)
    sha, _ = _archive(root)
    _json(profile / "modsIndex/main.json", {"mods": {sha: _entry()}})
    _json(root / "local/configuration", {"thumbnail_approximate_algorithm": "similarity only"})
    _image(profile / "thumbnail/Unit.png")
    (profile / "thumbnail/_redirection.ini").write_text("[recursive] pack\n", encoding="utf-8")
    result = _scan(reader, root)
    assert any(category.thumbnail_path for category in result.categories)
    assert any(issue.code == "warning" for issue in result.issues)


def test_preview_falls_back_to_reading_work_images_without_creating_shared_copies(tmp_path):
    reader = _reader()
    root, profile = _layout(tmp_path)
    sha, _ = _archive(root)
    _json(profile / "modsIndex/main.json", {"mods": {sha: _entry()}})
    first = _image(profile / "work/Mods" / sha / "preview.png")
    second = _image(profile / "work/Mods" / sha / "preview_screen.jpg")
    result = _scan(reader, root)
    assert result.mods[0].preview_paths == (str(first), str(second))
    assert not (root / "resources/preview").exists()
    assert not (root / "resources/preview_screen").exists()


def test_multiple_source_urls_preserve_the_first_and_report_omitted_alternatives(tmp_path):
    reader = _reader()
    root, profile = _layout(tmp_path)
    sha, _ = _archive(root)
    _json(
        profile / "modsIndex/main.json",
        {
            "mods": {
                sha: _entry(
                    get=[
                        {"url": "https://example.invalid/first", "mode": "get"},
                        {"url": "https://example.invalid/second", "mode": "lanzou"},
                    ]
                )
            }
        },
    )
    result = _scan(reader, root)
    assert result.mods[0].source_url == "https://example.invalid/first"
    assert any(issue.code == "warning" for issue in result.issues)


def test_unresolved_url_variables_are_reported_without_blocking_local_archive(tmp_path):
    reader = _reader()
    root, profile = _layout(tmp_path)
    sha, _ = _archive(root)
    _json(
        profile / "modsIndex/main.json",
        {
            "mods": {
                sha: _entry(
                    get=[
                        {
                            "url": "($.missing)/item",
                            "mode": "get",
                        }
                    ]
                )
            }
        },
    )
    result = _scan(reader, root)
    assert len(result.mods) == 1
    assert result.mods[0].source_url == ""
    assert any(issue.code == "warning" for issue in result.issues)


def test_cancel_before_scanning_raises_interrupted_error(tmp_path):
    reader = _reader()
    root, _ = _layout(tmp_path)
    with pytest.raises(InterruptedError):
        _scan(reader, root, should_cancel=lambda: True)


def test_progress_allows_cancelling_before_the_next_mod(tmp_path):
    reader = _reader()
    root, profile = _layout(tmp_path)
    mods = {}
    for content in ("first", "second", "third"):
        sha, _ = _archive(root, content)
        mods[sha] = _entry(name=content)
    _json(profile / "modsIndex/main.json", {"mods": mods})
    updates = []

    def progress(completed, total, message):
        """在首项处理完成后请求取消，验证扫描检查取消而非继续读取。"""
        updates.append((completed, total, message))

    with pytest.raises(InterruptedError):
        _scan(reader, root, progress=progress, should_cancel=lambda: any(row[0] == 1 for row in updates))
    assert updates[-1][0:2] == (1, 3)


def test_forged_environment_cannot_scan_another_installation(tmp_path):
    reader = _reader()
    root, _ = _layout(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    environment = reader.LegacyEnvironment("outside", "outside", str(outside), str(root))
    with pytest.raises(ValueError):
        reader.scan_environment(environment)


def test_archive_symlink_outside_root_is_reported_and_not_importable(tmp_path):
    reader = _reader()
    root, profile = _layout(tmp_path)
    outside = tmp_path / "outside.zip"
    outside.write_bytes(b"outside")
    sha = hashlib.sha1(outside.read_bytes()).hexdigest()
    _json(profile / "modsIndex/main.json", {"mods": {sha: _entry()}})
    _symlink(root / "resources/mods" / sha, outside)
    result = _scan(reader, root)
    assert result.mods == ()
    assert any(issue.code == "invalid" for issue in result.issues)


def test_linked_index_directory_and_cycle_are_not_followed(tmp_path):
    reader = _reader()
    root, profile = _layout(tmp_path)
    sha, _ = _archive(root)
    _json(profile / "modsIndex/local.json", {"mods": {sha: _entry()}})
    outside = tmp_path / "outside"
    _json(outside / "hidden.json", {"mods": {sha: _entry(name="Conflicting")}})
    _symlink(profile / "modsIndex/external", outside, directory=True)
    _symlink(profile / "modsIndex/cycle", profile / "modsIndex", directory=True)
    result = _scan(reader, root)
    assert [mod.name for mod in result.mods] == ["Sample Mod"]
    assert any(issue.code == "invalid" for issue in result.issues)


def test_unreadable_classification_does_not_reassign_mods_to_uncategorized(tmp_path):
    reader = _reader()
    root, profile = _layout(tmp_path)
    sha, _ = _archive(root)
    _json(profile / "modsIndex/main.json", {"mods": {sha: _entry()}})
    (profile / "classification/Collection").write_bytes(b"\xff\xfe\x00")
    result = _scan(reader, root)
    assert result.mods == ()
    assert not any(category.node_type == "character" for category in result.categories)
    assert any(issue.code == "invalid" for issue in result.issues)


@pytest.mark.parametrize("line", ["Unit A = ", "= selected.png", "Unit A = invalid\0path.png"])
def test_malformed_thumbnail_directives_are_reported_without_aborting_mod_scan(tmp_path, line):
    reader = _reader()
    root, profile = _layout(tmp_path)
    sha, _ = _archive(root)
    _json(profile / "modsIndex/main.json", {"mods": {sha: _entry()}})
    (profile / "thumbnail").mkdir()
    (profile / "thumbnail/_redirection.ini").write_text(line, encoding="utf-8")
    result = _scan(reader, root)
    assert len(result.mods) == 1
    assert result.issues


def test_unsupported_get_mode_preserves_provenance_and_reports_the_custom_configuration(tmp_path):
    reader = _reader()
    root, profile = _layout(tmp_path)
    sha, _ = _archive(root)
    _json(
        profile / "modsIndex/main.json",
        {
            "mods": {
                sha: _entry(
                    get=[
                        {
                            "url": "https://example.invalid/item",
                            "mode": "custom-handler",
                            "token": "sample-value",
                        }
                    ]
                )
            }
        },
    )
    result = _scan(reader, root)
    assert result.mods[0].source_url == "https://example.invalid/item"
    assert any(issue.code == "warning" for issue in result.issues)


def test_disabled_index_with_original_archive_is_importable(tmp_path):
    reader = _reader()
    root, profile = _layout(tmp_path)
    sha, archive = _archive(root)
    _json(profile / "modsIndex/disabled-folder/disabled-collection.json", {"mods": {sha: _entry()}})
    result = _scan(reader, root)
    assert len(result.mods) == 1
    assert result.mods[0].source_path == str(archive)
    assert any("停用" in issue.message for issue in result.issues)


def test_cache_only_source_is_recoverable_and_warned(tmp_path):
    reader = _reader()
    root, profile = _layout(tmp_path)
    sha = "b" * 40
    cache = profile / "work/Mods" / ("disabled-" + sha)
    cache.mkdir(parents=True)
    (cache / "unit.ini").write_text("modified", encoding="utf-8")
    _json(profile / "modsIndex/main.json", {"mods": {sha: _entry()}})
    result = _scan(reader, root)
    assert len(result.mods) == 1
    mod = result.mods[0]
    assert mod.source_path == mod.cache_path == str(cache)
    assert not mod.archive_path
    assert len(mod.cache_digest) == 64
    assert any("缓存" in issue.message for issue in result.issues)


@pytest.mark.parametrize("cache_content, changed", [("sample", False), ("edited", True)])
def test_original_is_preferred_and_cache_differences_are_exposed(tmp_path, cache_content, changed):
    reader = _reader()
    root, profile = _layout(tmp_path)
    sha, archive = _archive(root)
    cache = profile / "work/Mods" / sha
    cache.mkdir(parents=True)
    (cache / "unit.ini").write_text(cache_content, encoding="utf-8")
    _json(profile / "modsIndex/main.json", {"mods": {sha: _entry()}})
    result = _scan(reader, root)
    mod = result.mods[0]
    assert mod.source_path == mod.archive_path == str(archive)
    assert mod.cache_path == str(cache)
    assert mod.cache_changed is changed
    assert mod.cache_size_bytes == len(cache_content)


def test_disabled_cache_without_original_is_not_restored(tmp_path):
    reader = _reader()
    root, profile = _layout(tmp_path)
    sha = "c" * 40
    cache = profile / "work/Mods" / sha
    cache.mkdir(parents=True)
    (cache / "unit.ini").write_text("cached", encoding="utf-8")
    _json(profile / "modsIndex/disabled-main.json", {"mods": {sha: _entry()}})
    result = _scan(reader, root)
    assert not result.mods
    assert any(issue.code == "missing" for issue in result.issues)


def test_preview_directories_and_extended_formats_are_deduplicated(tmp_path):
    from PIL import Image

    reader = _reader()
    root, profile = _layout(tmp_path)
    sha, _ = _archive(root)
    _json(profile / "modsIndex/main.json", {"mods": {sha: _entry()}})
    directory = profile / "work/Mods" / ("disabled-" + sha) / "preview"
    directory.mkdir(parents=True)
    first = directory / "preview_1.png"
    Image.new("RGB", (8, 8), "red").save(first)
    (directory / "preview_copy.png").write_bytes(first.read_bytes())
    Image.new("RGB", (8, 8), "blue").save(directory / "preview_2.webp")
    Image.new("RGB", (8, 8), "green").save(directory.parent / "texture.png")
    result = _scan(reader, root)
    assert len(result.mods[0].preview_paths) == 2
    assert not any(Path(path).name == "texture.png" for path in result.mods[0].preview_paths)


def test_configured_approximate_thumbnail_matching_is_preserved(tmp_path):
    reader = _reader()
    root, profile = _layout(tmp_path)
    sha, _ = _archive(root)
    _json(profile / "modsIndex/main.json", {"mods": {sha: _entry(object="Unit A costume")}})
    _json(root / "local/configuration", {"thumbnail_approximate_algorithm": "key-in only"})
    image = _image(profile / "thumbnail/Unit A.png")
    result = _scan(reader, root)
    leaf = next(category for category in result.categories if category.node_type == "character")
    assert leaf.thumbnail_path == str(image)


@pytest.mark.parametrize("mode", [{"invalid": "configuration"}, ["similarity only"], 4, True])
def test_invalid_thumbnail_algorithm_does_not_abort_mod_scan(tmp_path, mode):
    reader = _reader()
    root, profile = _layout(tmp_path)
    sha, _ = _archive(root)
    _json(profile / "modsIndex/main.json", {"mods": {sha: _entry()}})
    _json(root / "local/configuration", {"thumbnail_approximate_algorithm": mode})
    _image(profile / "thumbnail/Unit.png")
    result = _scan(reader, root)
    assert len(result.mods) == 1
    assert any("匹配方式不受支持" in issue.message for issue in result.issues)


def test_nested_named_previews_are_found_without_sweeping_textures(tmp_path):
    from PIL import Image

    reader = _reader()
    root, profile = _layout(tmp_path)
    sha, _ = _archive(root)
    _json(profile / "modsIndex/main.json", {"mods": {sha: _entry()}})
    directory = profile / "work/Mods" / sha / "outfit/details"
    directory.mkdir(parents=True)
    Image.new("RGB", (8, 8), "red").save(directory / "preview_alternate.png")
    Image.new("RGB", (8, 8), "blue").save(directory / "texture.png")
    result = _scan(reader, root)
    assert [Path(path).name for path in result.mods[0].preview_paths] == ["preview_alternate.png"]


def test_linked_cache_is_not_offered_as_recovery(tmp_path):
    reader = _reader()
    root, profile = _layout(tmp_path)
    sha, _ = _archive(root)
    _json(profile / "modsIndex/main.json", {"mods": {sha: _entry()}})
    outside = tmp_path / "outside-cache"
    outside.mkdir()
    (outside / "mod.ini").write_text("outside", encoding="utf-8")
    path = profile / "work/Mods" / sha
    path.parent.mkdir(parents=True)
    _symlink(path, outside, directory=True)
    result = _scan(reader, root)
    assert len(result.mods) == 1
    assert not result.mods[0].cache_path
    assert any(issue.code == "invalid" for issue in result.issues)


@pytest.mark.parametrize("content, changed", [("original", False), ("modified", True)])
def test_7z_cache_comparison_reads_members_without_extracting(tmp_path, content, changed):
    import py7zr

    reader = _reader()
    root, profile = _layout(tmp_path)
    original = tmp_path / "unit.ini"
    original.write_text("original", encoding="utf-8")
    archive = tmp_path / "original.7z"
    with py7zr.SevenZipFile(archive, "w") as package:
        package.write(original, "unit.ini")
    payload = archive.read_bytes()
    sha = hashlib.sha1(payload).hexdigest()
    (root / "resources/mods" / sha).write_bytes(payload)
    cache = profile / "work/Mods" / sha
    cache.mkdir(parents=True)
    (cache / "unit.ini").write_text(content, encoding="utf-8")
    _json(profile / "modsIndex/main.json", {"mods": {sha: _entry(type="7z")}})
    before = _snapshot(root)
    result = _scan(reader, root)
    assert result.mods[0].cache_changed is changed
    assert _snapshot(root) == before
