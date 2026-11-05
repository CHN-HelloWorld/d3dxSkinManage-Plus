"""类别编辑的类型选择、冲突确认及后台全部禁用后的转换。"""

from __future__ import annotations

from threading import Event, Lock

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QMessageBox

from src.core.deployment_preparation import DeploymentPreparation
from src.ui.pages.overview import CharacterDialog
from tests.core.test_node_type_conversion import category_tree as category_tree
from tests.core.test_node_type_conversion import mm as mm
from tests.core.test_variant_preset_switch import env as env
from tests.qt_dialogs import interact
from tests.ui.test_disable_preset_lifecycle import _manager_window


@pytest.fixture
def edit_window(qtbot, mm):
    """保留真实禁用线程与索引提交，只替换无关的页面重建。"""
    window = _manager_window(qtbot, mm)
    window._load_characters = lambda game_id: None
    yield window
    if window._disable_worker is not None:
        window._disable_worker.requestInterruption()
        window._disable_worker.wait(5000)
        qtbot.waitUntil(lambda: window._disable_worker is None, timeout=5000)
    window.close()


@pytest.mark.parametrize("source,target", [("category", "character"), ("character", "category")])
def test_edit_dialog_exposes_and_submits_node_type(qtbot, temp_data_dir, source, target):
    dialog = CharacterDialog({"id": "node", "name": "Node", "node_type": source})
    qtbot.addWidget(dialog)
    dialog.show()

    assert dialog._type_combo.isVisible()
    assert dialog._type_combo.currentData() == source
    dialog._type_combo.setCurrentIndex(dialog._type_combo.findData(target))
    dialog._on_ok()

    assert dialog.get_result()["node_type"] == target
    assert dialog.result() == dialog.DialogCode.Accepted


def test_conversion_keeps_another_character_with_the_same_name_in_validation(qtbot, temp_data_dir, monkeypatch):
    dialog = CharacterDialog(
        {"id": "node", "name": "Shared", "node_type": "category"},
        existing_names=["Shared"],
        all_character_names=["Shared"],
    )
    qtbot.addWidget(dialog)
    dialog.show()
    dialog._type_combo.setCurrentIndex(dialog._type_combo.findData("character"))
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda parent, title, text: warnings.append(text))
    dialog._on_ok()
    assert dialog.get_result() is None
    assert dialog.isVisible()
    assert warnings == ['当前游戏中已存在名为 "Shared" 的角色/对象，请使用其他名称。']


def _choose_conflict(dialog, button_text):
    """通过真实提示按钮作出选择，同时检查两个选项及完整的子树提示。"""
    assert dialog.text() == "此节点下当前启用了多个Mod，请仅保留一个启用的Mod，然后再试。"
    buttons = {button.text(): button for button in dialog.buttons()}
    assert set(buttons) == {"全部禁用并重试", "确认"}
    buttons[button_text].click()


def test_confirming_conflict_keeps_node_and_all_enabled_mods(edit_window, mm, category_tree):
    root, mods = category_tree
    with interact(QMessageBox, lambda dialog: _choose_conflict(dialog, "确认")):
        edit_window._on_character_edit(root.id, {"name": "Renamed", "node_type": "character"})

    assert mm.get_character(root.id).node_type == "category"
    assert mm.get_character(root.id).name == "Branch"
    assert all(mm.get_mod(mod.mod_id).is_enabled for mod in mods)
    assert edit_window._disable_worker is None


@pytest.mark.parametrize("outcome", ["success", "cancel", "file_error", "commit_error"])
def test_conversion_waits_for_complete_successful_background_disable(
    edit_window, mm, category_tree, monkeypatch, qtbot, outcome
):
    root, mods = category_tree
    entered, release = Event(), Event()
    real_prepare = DeploymentPreparation.prepare

    def prepare(preparation):
        """暂停文件准备，让主线程检查节点尚未改动并模拟取消。"""
        entered.set()
        assert release.wait(5)
        return real_prepare(preparation)

    monkeypatch.setattr(DeploymentPreparation, "prepare", prepare)
    if outcome == "file_error":
        real_undeploy = mm._storage.undeploy_mod

        def undeploy(source_path, deploy_name="", **kwargs):
            """模拟第一份部署仍被占用，其余项目保持真实的删除过程。"""
            if deploy_name == mods[0].folder_name:
                return False
            return real_undeploy(source_path, deploy_name, **kwargs)

        monkeypatch.setattr(mm._storage, "undeploy_mod", undeploy)
    elif outcome == "commit_error":

        def fail_commit(results):
            """模拟文件处理后索引写入失败，禁止继续保存节点。"""
            raise OSError("database unavailable")

        monkeypatch.setattr(mm, "commit_disable_batch", fail_commit)

    try:
        with interact(QMessageBox, lambda dialog: _choose_conflict(dialog, "全部禁用并重试")):
            edit_window._on_character_edit(root.id, {"name": "Renamed", "node_type": "character"})
        heartbeats = []
        QTimer.singleShot(0, lambda: heartbeats.append(True))
        qtbot.waitUntil(lambda: entered.is_set() and bool(heartbeats), timeout=2000)
        assert mm.get_character(root.id).node_type == "category"
        assert mm.get_character(root.id).name == "Branch"
        if outcome == "cancel":
            edit_window._on_disable_stop_requested()
        release.set()
        qtbot.waitUntil(lambda: edit_window._disable_worker is None, timeout=5000)

        current = mm.get_character(root.id)
        assert current.node_type == ("character" if outcome == "success" else "category")
        assert current.name == ("Renamed" if outcome == "success" else "Branch")
        assert mm.get_mod(mods[2].mod_id).is_enabled
        if outcome == "success":
            assert mm.get_enabled_mod_ids(root.id) == []
        elif outcome == "file_error":
            assert mm.get_mod(mods[0].mod_id).is_enabled
            assert not mm.get_mod(mods[1].mod_id).is_enabled
        elif outcome == "cancel":
            assert all(mm.get_mod(mod.mod_id).is_enabled for mod in mods)
        assert mm._library_protection.snapshot() == (set(), set())
    finally:
        release.set()


def test_nested_character_remains_inside_converted_parent_conflict_scope(edit_window, mm, category_tree):
    root, mods = category_tree
    assert mm.toggle_mod(mods[1].mod_id) is not None
    assert mm.update_character(root.id, root.name, node_type="character")

    ancestor, scope_ids = edit_window._character_conflict_scope(mm.get_mod(mods[1].mod_id))
    conflicts = mm.get_character_conflict_mods(scope_ids, excluding_mod_id=mods[1].mod_id)

    assert ancestor.id == root.id
    assert [mod.mod_id for mod in conflicts] == [mods[0].mod_id]
    assert mm.find_character_ancestor(mods[1].node_id).id == mods[1].node_id

    assert mm.update_character(root.id, root.name, node_type="category")
    ancestor, scope_ids = edit_window._character_conflict_scope(mm.get_mod(mods[1].mod_id))
    assert ancestor.id == mods[1].node_id
    assert mm.get_character_conflict_mods(scope_ids, excluding_mod_id=mods[1].mod_id) == []


def test_pending_deployments_cannot_publish_multiple_mods_into_a_converted_character(env, qtbot, monkeypatch):
    """暂停两个真实启用线程，验证分类转换会等待部署结束，避免用旧类别语义提交双启用。"""
    from tests.core.test_named_variant_coordination import add_mod
    from tests.ui.test_enable_publication import _real_deploy_window

    manager, parent, first = env["mm"], env["char"], env["mod"]
    assert manager.update_character(parent.id, parent.name, node_type="category")
    second = add_mod(env, "Second", "[Constants]\nglobal $other = 0\n")
    window = _real_deploy_window(env, qtbot)
    window._config.set("mods.deploy_concurrency", 2)
    window._load_characters = lambda game_id: None
    entered, release, lock = [], Event(), Lock()
    real_materialize = manager._storage.materialize_deployment
    notices = []
    monkeypatch.setattr(QMessageBox, "information", lambda parent, title, text: notices.append(text))

    def pause_materialize(*args, **kwargs):
        """保留真实操作登记与结果发布，仅暂停磁盘准备步骤。"""
        with lock:
            entered.append(args[1])
        assert release.wait(10)
        return real_materialize(*args, **kwargs)

    monkeypatch.setattr(manager._storage, "materialize_deployment", pause_materialize)
    try:
        window._start_async_deploy(first.mod_id, [])
        window._start_async_deploy(second.mod_id, [])
        qtbot.waitUntil(lambda: len(entered) == 2, timeout=3000)
        assert manager.get_enabled_mod_ids(parent.id) == []
        window._on_character_edit(parent.id, {"name": parent.name, "node_type": "character", "thumbnail": None})
        assert manager.get_character(parent.id).node_type == "category"
        assert notices == ["Mod 正在后台部署，请等待部署完成后重试。"]
        release.set()
        qtbot.waitUntil(lambda: not window._deploy_workers and not window._enable_publication.busy, timeout=15000)
        assert manager.get_character(parent.id).node_type == "category"
        assert set(manager.get_enabled_mod_ids(parent.id)) == {first.mod_id, second.mod_id}
    finally:
        release.set()
        for worker in list(window._deploy_workers.values()):
            worker.wait(10000)
        qtbot.waitUntil(lambda: not window._deploy_workers and not window._enable_publication.busy, timeout=15000)
