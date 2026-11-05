"""节点类型转换须保持子树启用约束、分类身份及其他分支的状态。"""

from __future__ import annotations

import json

import pytest

from src.utils.constants import CATEGORY_MARKER_FILE
from tests.core.test_library_reconcile import _import_mod
from tests.core.test_library_reconcile import mm as mm


@pytest.fixture
def category_tree(mm, tmp_path, request):
    """建立含两层后代和独立分支的真实部署库，子树内外分别启用 Mod。"""
    root = mm.add_character("Branch")
    child = mm.add_character("Child", parent_id=root.id)
    leaf = mm.add_character("Leaf", parent_id=child.id, node_type="character")
    outside = mm.add_character("Outside")
    first_node = root if getattr(request, "param", "descendants") == "mixed" else child
    mm.set_deploy_dir(str(tmp_path / "deployment"))
    mods = [
        _import_mod(mm, first_node.id, tmp_path, "First"),
        _import_mod(mm, leaf.id, tmp_path, "Second"),
        _import_mod(mm, outside.id, tmp_path, "Unrelated"),
    ]
    for mod in mods:
        enabled = mm.toggle_mod(mod.mod_id)
        assert enabled is not None and enabled.is_enabled
    return root, mods


@pytest.mark.parametrize("category_tree", ["mixed", "descendants"], indirect=True)
def test_multiple_enabled_descendants_reject_conversion_before_any_mutation(mm, category_tree):
    root, mods = category_tree
    old_path = mm._get_character_library(root.id)
    marker = old_path / CATEGORY_MARKER_FILE
    before = marker.read_bytes()

    assert not mm.update_character(root.id, "Renamed", "English", node_type="character")

    current = mm.get_character(root.id)
    assert (current.name, current.name_en, current.node_type) == ("Branch", "", "category")
    assert marker.read_bytes() == before
    assert not (old_path.parent / "Renamed").exists()
    assert all(mm.get_mod(mod.mod_id).is_enabled for mod in mods)


@pytest.mark.parametrize("enabled_count", [0, 1])
def test_conversion_with_at_most_one_enabled_descendant_preserves_identity(mm, category_tree, enabled_count):
    root, mods = category_tree
    for mod in mods[enabled_count:2]:
        disabled = mm.toggle_mod(mod.mod_id)
        assert disabled is not None and not disabled.is_enabled

    assert mm.update_character(root.id, "Renamed", "English", node_type="character")

    current = mm.get_character(root.id)
    assert (current.name, current.name_en, current.node_type) == ("Renamed", "English", "character")
    marker = json.loads((mm._get_character_library(root.id) / CATEGORY_MARKER_FILE).read_text(encoding="utf-8"))
    assert marker["id"] == root.id and marker["node_type"] == "character"
    assert len(mm.get_enabled_mod_ids(root.id)) == enabled_count
    assert mm.get_mod(mods[2].mod_id).is_enabled


def test_character_can_be_converted_back_without_disabling_its_mod(mm, category_tree):
    root, mods = category_tree
    assert mm.toggle_mod(mods[1].mod_id) is not None
    assert mm.update_character(root.id, root.name, node_type="character")

    assert mm.update_character(root.id, root.name, node_type="category")

    assert mm.get_character(root.id).node_type == "category"
    assert mm.get_mod(mods[0].mod_id).is_enabled
    assert mm.get_mod(mods[2].mod_id).is_enabled


def test_category_metadata_edits_do_not_apply_character_exclusivity(mm, category_tree):
    root, mods = category_tree

    assert mm.update_character(root.id, root.name, "English", node_type="category")

    assert mm.get_character(root.id).name_en == "English"
    assert all(mm.get_mod(mod.mod_id).is_enabled for mod in mods)


@pytest.mark.parametrize("protected_index,allowed", [(0, False), (2, True)])
def test_conversion_waits_for_operations_in_its_own_subtree(mm, category_tree, protected_index, allowed):
    root, mods = category_tree
    for mod in mods[:2]:
        assert mm.toggle_mod(mod.mod_id) is not None
    pending = mm.get_mod(mods[protected_index].mod_id)
    mm.protect_library_operation("pending-enable", paths=[pending.path], mod_ids=[pending.mod_id])
    try:
        assert mm.update_character(root.id, root.name, node_type="character") is allowed
        assert mm.get_character(root.id).node_type == ("character" if allowed else "category")
    finally:
        mm.release_library_operation("pending-enable")
