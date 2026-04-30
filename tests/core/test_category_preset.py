from __future__ import annotations

import copy

import pytest

from src.core.category_preset import CategoryPresetError, validate_manifest


def manifest():
    return {
        "kind": "d3dxSkinManage-Plus.category-preset",
        "nodes": [
            {"path": "角色", "node_type": "category"},
            {"path": "角色/示例角色丁", "node_type": "character"},
            {"path": "角色/示例角色未", "node_type": "character"},
            {"path": "武器", "node_type": "category"},
        ],
    }


def validate(data, images_length=0):
    return validate_manifest(data, fallback_name="测试预设", images_length=images_length)


def test_defaults_follow_sibling_order_and_physical_leaf_name():
    document = validate(manifest())
    assert document.name == "测试预设"
    assert document.suggested_game_name == document.suggested_game_name_en == ""
    assert document.images == ()
    assert [(n.name, n.name_en, n.sort_order, n.image_id) for n in document.nodes] == [
        ("角色", "", 0, ""),
        ("示例角色丁", "", 0, ""),
        ("示例角色未", "", 1, ""),
        ("武器", "", 1, ""),
    ]


def test_unknown_fields_are_ignored_at_every_level():
    data = manifest()
    data.update(
        version=999,
        schema_version="future",
        destination="C:/outside",
        suggested_game={
            "name": "示例游戏甲",
            "name_en": "ExampleGameA",
            "local_game_id": "not-local",
        },
    )
    data["nodes"][0].update(local_id="foreign", absolute_path="C:/outside")
    document = validate(data)
    assert document.suggested_game_name == "示例游戏甲"
    assert document.nodes[0].path == "角色"
    assert not hasattr(document.nodes[0], "local_id")


@pytest.mark.parametrize(
    "field,value",
    [
        ("kind", None),
        ("kind", "other"),
        ("scope", "folder"),
        ("scope", None),
        ("name", ""),
        ("name", None),
        ("suggested_game", []),
        ("suggested_game", {"name": None}),
        ("suggested_game", {"name_en": False}),
        ("nodes", []),
        ("nodes", {}),
        ("images", None),
    ],
)
def test_invalid_top_level_known_fields_are_not_defaulted(field, value):
    data = manifest()
    data[field] = value
    with pytest.raises(CategoryPresetError):
        validate(data)


@pytest.mark.parametrize(
    "field,value",
    [
        ("path", ""),
        ("path", None),
        ("name", ""),
        ("name", None),
        ("name_en", 1),
        ("node_type", "mod"),
        ("node_type", None),
        ("sort_order", True),
        ("sort_order", 1.5),
        ("image_id", None),
        ("image_id", "missing"),
    ],
)
def test_invalid_node_fields_are_rejected(field, value):
    data = manifest()
    data["nodes"][0][field] = value
    with pytest.raises(CategoryPresetError):
        validate(data)


@pytest.mark.parametrize(
    "path",
    [
        "/root",
        "C:/root",
        "//server/share",
        "a\\b",
        "a//b",
        "a/",
        ".",
        "..",
        "a/../b",
        "a:b",
        "a*",
        "a?",
        'a"',
        "a<",
        "a>",
        "a|",
        "a\x00",
        "a\x1f",
        "a.",
        "a ",
        ".hidden",
        ".d3dxmeta",
        "CON",
        "nul.txt",
        "COM1",
        "lpt9.dat",
        "COM¹",
        "A" * 201,
    ],
)
def test_unsafe_windows_path_segments_are_rejected(path):
    data = manifest()
    data["nodes"] = [{"path": path, "node_type": "category"}]
    with pytest.raises(CategoryPresetError):
        validate(data)


def test_missing_parent_and_windows_case_collisions_are_rejected():
    for paths in [("A/B",), ("Root", "root"), ("Root", "Root/A", "root/a")]:
        data = manifest()
        data["nodes"] = [{"path": p, "node_type": "category"} for p in paths]
        with pytest.raises(CategoryPresetError):
            validate(data)


@pytest.mark.parametrize("path", ["CONIN$", "conout$.txt", "CON .txt", "LPT1  .png"])
def test_windows_device_aliases_cannot_be_used_as_directory_names(path):
    data = manifest()
    data["nodes"] = [{"path": path, "node_type": "category"}]
    with pytest.raises(CategoryPresetError):
        validate(data)


@pytest.mark.parametrize("path", ["😀" * 127, "😀" * 127 + "a", "😀" * 55 + "a" * 145, "a" * 200])
def test_windows_component_utf16_and_code_point_boundaries_accept_valid_names(path):
    data = manifest()
    data["nodes"] = [{"path": path, "node_type": "category"}]
    assert validate(data).nodes[0].path == path


@pytest.mark.parametrize("path", ["😀" * 128, "😀" * 127 + "ab", "😀" * 56 + "a" * 144, "a" * 201])
def test_windows_component_utf16_and_code_point_boundaries_reject_invalid_names(path):
    data = manifest()
    data["nodes"] = [{"path": path, "node_type": "category"}]
    with pytest.raises(CategoryPresetError):
        validate(data)


def test_parent_lookup_uses_windows_directory_key_and_preserves_input_order():
    data = manifest()
    data["nodes"] = [{"path": "ROOT/Child", "node_type": "character"}, {"path": "Root", "node_type": "category"}]
    assert [n.path for n in validate(data).nodes] == ["ROOT/Child", "Root"]


def test_node_count_and_path_depth_limits():
    data = manifest()
    data["nodes"] = [{"path": f"n{i}", "node_type": "category"} for i in range(5000)]
    assert len(validate(data).nodes) == 5000
    data["nodes"].append({"path": "extra", "node_type": "category"})
    with pytest.raises(CategoryPresetError):
        validate(data)
    data["nodes"] = [{"path": "/".join(["a"] * i), "node_type": "category"} for i in range(1, 65)]
    assert len(validate(data).nodes) == 64
    data["nodes"].append({"path": "/".join(["a"] * 65), "node_type": "category"})
    with pytest.raises(CategoryPresetError):
        validate(data)


def image_manifest():
    data = manifest()
    data["nodes"][0]["image_id"] = "a" * 64
    data["images"] = [{"id": "a" * 64, "offset": 0, "length": 12, "media_type": "image/png"}]
    return data


def test_image_index_accepts_shared_reference_and_unknown_fields():
    data = image_manifest()
    data["nodes"][1]["image_id"] = "a" * 64
    data["images"][0]["output_path"] = "../../outside.exe"
    document = validate(data, 12)
    assert len(document.images) == 1
    assert document.images[0].image_id == "a" * 64


@pytest.mark.parametrize(
    "field,value",
    [
        ("id", "A" * 64),
        ("id", "a" * 63),
        ("offset", -1),
        ("offset", True),
        ("offset", 1),
        ("length", 0),
        ("length", False),
        ("length", 13),
        ("length", 20 * 1024 * 1024 + 1),
        ("media_type", "image/svg+xml"),
    ],
)
def test_invalid_image_index_is_rejected(field, value):
    data = image_manifest()
    data["images"][0][field] = value
    with pytest.raises(CategoryPresetError):
        validate(data, 12)


def test_image_index_requires_all_fields_and_contiguous_referenced_entries():
    for field in ("id", "offset", "length", "media_type"):
        data = image_manifest()
        del data["images"][0][field]
        with pytest.raises(CategoryPresetError):
            validate(data, 12)
    for offset in (0, 11, 13):
        data = image_manifest()
        data["nodes"][1]["image_id"] = "b" * 64
        data["images"].append({"id": "b" * 64, "offset": offset, "length": 12, "media_type": "image/png"})
        with pytest.raises(CategoryPresetError):
            validate(data, 24)
    data = image_manifest()
    del data["nodes"][0]["image_id"]
    with pytest.raises(CategoryPresetError):
        validate(data, 12)
    data = image_manifest()
    data["images"].append(copy.deepcopy(data["images"][0]))
    data["images"][1]["offset"] = 12
    with pytest.raises(CategoryPresetError):
        validate(data, 24)


def test_unknown_nested_fields_still_obey_depth_and_finite_number_limits():
    data = manifest()
    data["extra"] = float("inf")
    with pytest.raises(CategoryPresetError):
        validate(data)
    nested = {}
    data["extra"] = nested
    for _ in range(16):
        nested["nested"] = {}
        nested = nested["nested"]
    with pytest.raises(CategoryPresetError):
        validate(data)
