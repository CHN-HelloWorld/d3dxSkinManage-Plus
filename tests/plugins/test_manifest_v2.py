"""验证插件清单的声明约束与语义版本区间。"""

import json

import pytest

from src.plugins.runtime.manifest import ManifestError, parse_manifest


def make_plugin(base, plugin_id="test.package", **changes):
    """在临时目录创建只包含公开 API 声明的插件包。"""
    directory = base / plugin_id
    directory.mkdir(parents=True, exist_ok=True)
    data = {
        "id": plugin_id,
        "name": "测试插件",
        "version": "1.2.3",
        "apiVersion": "2.0",
        "entry": "entry.py",
        "permissions": [],
        "contributes": {},
    }
    data.update(changes)
    (directory / "plugin.json").write_text(json.dumps(data), encoding="utf-8")
    (directory / "entry.py").write_text("VALUE = 1\n", encoding="utf-8")
    return directory


@pytest.mark.parametrize(
    "change",
    [
        {"name": 23},
        {"version": "latest"},
        {"permissions": ["unknown:permission"]},
        {"activationEvent": ["onStartup"]},
        {"activationEvents": ["anything"]},
        {"activationEvents": ["onEvent:no_such_event"]},
        {"activationEvents": ["onCommand:other.command"]},
        {"dependencies": {"test.package": "*"}},
        {"dependencies": {"test.other": "not a version"}},
        {"contributes": {"commands": [{"id": "other.command"}]}},
        {"contributes": {"commands": [{"id": "test.package.run"}, {"id": "test.package.run"}]}},
        {"contributes": {"toolsPage": [{"command": "test.package.missing"}]}},
        {"contributes": {"views": [{"id": "view"}]}},
        {"contributes": {"typo": []}},
        {"files": ["../outside"]},
        {"entry": "entry.py", "files": ["missing.py"]},
    ],
)
def test_manifest_rejects_invalid_declarations_before_import(tmp_path, change):
    directory = make_plugin(tmp_path, **change)
    with pytest.raises(ManifestError):
        parse_manifest(directory)


def test_manifest_keeps_dependencies_and_infers_command_activation(tmp_path):
    directory = make_plugin(
        tmp_path,
        dependencies={"test.provider": "^1.0.0"},
        activationEvents=["onEvent:game_changed"],
        contributes={"commands": [{"id": "test.package.run", "title": "执行"}]},
        files=["entry.py"],
    )
    manifest = parse_manifest(directory)
    assert manifest.dependencies == {"test.provider": "^1.0.0"}
    assert set(manifest.activation_events) == {"onEvent:game_changed", "onCommand:test.package.run"}
    assert manifest.files == ("entry.py",)


@pytest.mark.parametrize(
    ("version", "constraint", "expected"),
    [
        ("1.4.0", "^1.2.0", True),
        ("2.0.0", "^1.2.0", False),
        ("0.3.0", "^0.2.0", False),
        ("0.0.5", "^0.0.4", False),
        ("1.2.9", "~1.2.0", True),
        ("1.3.0", "~1.2.0", False),
        ("1.4.0", ">=1.2.0 <2.0.0", True),
        ("1.0.0+build.7", "1.0.0", True),
        ("1.1.0-beta.2", "*", False),
        ("1.1.0-beta.2", ">=1.1.0-beta.1 <1.1.0", True),
        ("1.1.0-beta.12", ">1.1.0-beta.2", True),
        ("1.1.0", ">1.1.0-beta.2", True),
    ],
)
def test_dependency_ranges_compare_semantic_versions(version, constraint, expected):
    from src.plugins.runtime.versions import satisfies

    assert satisfies(version, constraint) is expected


@pytest.mark.parametrize("version", ["01.0.0", "1.0", "1.0.0-01", "1.0.0-", "1.0.0+"])
def test_version_parser_rejects_ambiguous_versions(version):
    from src.plugins.runtime.versions import Version

    with pytest.raises(ValueError):
        Version.parse(version)
