"""验证教学插件的清单内容、预设引用检查和轮换边界。"""

from __future__ import annotations

import csv
import io
from pathlib import Path
from runpy import run_path

import pytest

from src.plugin_sdk.facades import CharacterInfo, ModInfo
from src.plugin_sdk.presets import PresetInfo


def example_module(plugin_id: str) -> dict:
    """从实际分发的入口读取插件，避免测试维护另一份示例实现。"""
    filename = "inventory.py" if plugin_id == "example.mod-inventory" else "entry.py"
    entry = Path(__file__).resolve().parents[2] / "plugins" / plugin_id / filename
    assert entry.is_file(), f"缺少教学插件：{plugin_id}"
    return run_path(str(entry))


@pytest.fixture
def inventory():
    """提供包含同名节点、子分类和分隔字符的独立 SDK 快照。"""
    characters = [
        CharacterInfo("root", "服装", game_id="game"),
        CharacterInfo("child", "角色甲", parent_id="root", game_id="game", node_type="character"),
        CharacterInfo("outside", "服装", game_id="game"),
    ]
    mods = [
        ModInfo("outside-mod", "外部", node_id="outside", game_id="game"),
        ModInfo("child-mod", '蓝色, "外观"\n第二行', '作者, "甲"', True, "child", "game", "G", ("蓝色", "日常")),
        ModInfo("root-mod", "基础", node_id="root", game_id="game", rating="P"),
    ]
    return mods, characters


def test_inventory_csv_includes_descendants_and_escapes_metadata(inventory):
    module = example_module("example.mod-inventory")
    content = module["build_inventory"](*inventory, node_id="root")
    rows = list(csv.DictReader(io.StringIO(content)))
    assert [row["mod_id"] for row in rows] == ["root-mod", "child-mod"]
    assert rows[1] == {
        "game_id": "game",
        "mod_id": "child-mod",
        "category": "服装 / 角色甲",
        "name": '蓝色, "外观"\n第二行',
        "author": '作者, "甲"',
        "enabled": "true",
        "rating": "G",
        "tags": "蓝色; 日常",
    }


def test_inventory_enabled_filter_and_empty_result_keep_csv_header(inventory):
    module = example_module("example.mod-inventory")
    content = module["build_inventory"](*inventory, enabled_only=True)
    assert [row["mod_id"] for row in csv.DictReader(io.StringIO(content))] == ["child-mod"]
    empty = module["build_inventory"](*inventory, node_id="outside", enabled_only=True)
    reader = csv.DictReader(io.StringIO(empty))
    assert list(reader) == []
    assert reader.fieldnames == ["game_id", "mod_id", "category", "name", "author", "enabled", "rating", "tags"]


@pytest.mark.parametrize("value", ["=1+1", "+SUM(1,2)", "-1+2", "@SUM(1,2)", "  =1+1", "\ttext", "\rtext"])
def test_inventory_spreadsheet_cells_remain_text(value):
    module = example_module("example.mod-inventory")
    content = module["build_inventory"]([ModInfo("m", value, game_id="g")], [])
    assert next(csv.DictReader(io.StringIO(content)))["name"] == "'" + value


def test_inventory_handles_missing_or_cyclic_category_ancestors():
    module = example_module("example.mod-inventory")
    nodes = [CharacterInfo("a", "甲", parent_id="b"), CharacterInfo("b", "乙", parent_id="a")]
    mods = [ModInfo("orphan", "失去分类", node_id="missing"), ModInfo("cycle", "循环", node_id="a")]
    content = module["build_inventory"](mods, nodes, node_id="b")
    assert [row["mod_id"] for row in csv.DictReader(io.StringIO(content))] == ["cycle"]
    content = module["build_inventory"](mods, nodes)
    assert len(list(csv.DictReader(io.StringIO(content)))) == 2


def test_inventory_file_is_utf8_with_bom_and_write_failure_preserves_existing_directory(tmp_path):
    module = example_module("example.mod-inventory")
    target = tmp_path / "清单.csv"
    module["write_inventory"](str(target), "name\r\n示例\r\n")
    assert target.read_bytes() == b"\xef\xbb\xbf" + "name\r\n示例\r\n".encode()
    directory = tmp_path / "directory.csv"
    directory.mkdir()
    sentinel = directory / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")
    with pytest.raises(OSError):
        module["write_inventory"](str(directory), "new")
    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_preset_audit_uses_mod_and_variant_ids_and_accepts_default_configuration():
    module = example_module("example.preset-audit")
    presets = [
        PresetInfo(
            "p1",
            "g",
            "组合甲",
            enabled_mod_ids=("missing", "m1", "m2"),
            mod_variants={"missing": "v0", "m1": "missing-variant", "m2": None},
        ),
        PresetInfo("p2", "g", "组合乙", enabled_mod_ids=("m1",), mod_variants={"m1": "v1"}),
    ]
    mods = [ModInfo("m1", "同名外观", game_id="g"), ModInfo("m2", "同名外观", game_id="g")]
    issues = module["audit_presets"](presets, mods, {"m1": {"v1"}, "m2": set()})
    assert [(issue.preset_id, issue.mod_id, issue.reason) for issue in issues] == [
        ("p1", "missing", "missing_mod"),
        ("p1", "m1", "missing_variant"),
    ]
    assert issues[0].mod_name == "missing"
    assert issues[1].mod_name == "同名外观"


def test_preset_audit_does_not_accept_a_mod_from_another_game():
    module = example_module("example.preset-audit")
    preset = PresetInfo("p", "g", "组合", enabled_mod_ids=("m",))
    issues = module["audit_presets"]([preset], [ModInfo("m", "外观", game_id="other")], {})
    assert [(issue.mod_id, issue.reason) for issue in issues] == [("m", "missing_mod")]


@pytest.mark.parametrize(
    ("current", "step", "include_default", "expected"),
    [
        ("default", 1, False, "z"),
        ("default", -1, False, "a"),
        ("z", 1, False, "a"),
        ("a", 1, False, "z"),
        ("z", -1, False, "a"),
        ("z", -1, True, "default"),
        ("removed", 1, False, "z"),
    ],
)
def test_preset_cycle_preserves_host_order_and_wraps(current, step, include_default, expected):
    module = example_module("example.preset-cycle")
    presets = [
        PresetInfo("default", "g", "默认", is_default=True),
        PresetInfo("z", "g", "Z"),
        PresetInfo("a", "g", "A"),
    ]
    result = module["select_preset"](presets, current, step, include_default=include_default)
    assert result.preset_id == expected


@pytest.mark.parametrize("presets", [[], [PresetInfo("current", "g", "当前")]])
def test_preset_cycle_does_not_reapply_the_only_current_choice(presets):
    module = example_module("example.preset-cycle")
    assert module["select_preset"](presets, "current", 1) is None
