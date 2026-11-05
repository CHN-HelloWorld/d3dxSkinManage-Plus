"""部署入口只定位当前 Mod 的已发布副本，并保持窄预览栏布局稳定。"""

from pathlib import Path

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QMenu, QPushButton, QWidget

from src.core.mod_identity import new_mod_id, write_deployment_identity
from src.ui.pages.overview import OverviewPage
from src.utils import i18n
from tests.core.test_library_reconcile import _import_mod
from tests.core.test_library_reconcile import mm as mm
from tests.qt_dialogs import interact
from tests.ui.test_theme_transition_surfaces import appearance as appearance


@pytest.fixture
def preview(qtbot, mm, tmp_path, monkeypatch, appearance, request):
    language, mode = getattr(request, "param", ("zh-CN", "dark"))
    catalog = i18n.I18n(directory=Path(__file__).resolve().parents[2] / "config" / "languages")
    catalog.set_language(language)
    monkeypatch.setattr(i18n, "_i18n", catalog)
    appearance(mode)
    root = tmp_path / "deployment"
    root.mkdir()
    mm.set_deploy_dir(str(root))
    source = tmp_path / "source"
    source.mkdir()
    (source / "mod.ini").write_text("[Constants]\nx = 1\n", encoding="utf-8")
    node = mm.add_character("Category")
    mod = mm.import_mod(source_path=str(source), name="Preview", node_id=node.id)
    assert mod is not None
    page = OverviewPage()
    qtbot.addWidget(page)
    page.set_mod_manager(mm)
    page.resize(1024, 680)
    page.set_characters(mm.get_characters_for_ui("game_a"), refresh=False)
    page._current_parent_id = node.id
    page.set_current_mods([mod])
    page._selected_ids = {mod.mod_id}
    page.show()
    page._show_selection_panel()
    yield page, mod, root
    page.close()


def deployment_button(page):
    button = page.findChild(QPushButton, "previewDeploymentButton")
    assert button is not None, "The preview has no deployment action"
    return button


def deploy(mod, root, name="Preview"):
    path = root / name
    write_deployment_identity(path, mod.mod_id, mod.content_hash)
    mod.remember_deployment(path)
    return path


@pytest.mark.parametrize(
    "name,enabled,visible",
    [
        ("Preview", True, True),
        ("DISABLED_Preview", False, True),
        ("DISABLED_Preview.partial", False, False),
        ("DISABLED_.update_Preview", False, False),
    ],
)
def test_only_published_deployments_have_an_action(preview, name, enabled, visible):
    page, mod, root = preview
    button = deployment_button(page)
    assert not button.isVisible()
    path = deploy(mod, root, name)
    if enabled:
        mod.enable()
    page.update_mod_toggle_in_place(mod.mod_id, mod)
    assert button.isVisible() is visible
    if visible:
        assert str(path) in button.toolTip()


@pytest.mark.parametrize("invalid", ["removed", "unmarked", "different_mod", "outside_root", "different_game"])
def test_stale_or_unrelated_deployments_are_hidden(preview, invalid, tmp_path):
    page, mod, root = preview
    button = deployment_button(page)
    path = deploy(mod, root)
    page._refresh_panel_mod_status()
    assert button.isVisible()
    if invalid == "removed":
        (path / ".d3dxmod").unlink()
        path.rmdir()
    elif invalid == "unmarked":
        (path / ".d3dxmod").unlink()
    elif invalid == "different_mod":
        write_deployment_identity(path, new_mod_id())
    elif invalid == "outside_root":
        deploy(mod, tmp_path / "old_deployment")
    else:
        mod.game_id = "game_b"
    page._refresh_panel_mod_status()
    assert not button.isVisible()


def test_deployment_completion_and_selection_changes_refresh_the_action(preview):
    page, mod, root = preview
    button = deployment_button(page)
    page.begin_deploy_overlay(mod.mod_id)
    assert not button.isVisible()
    deploy(mod, root)
    mod.enable()
    page.finish_deploy_overlay(mod.mod_id)
    assert button.isVisible()
    for clear in (
        page._show_multi_select_panel,
        lambda: page._show_category_panel({"id": "category", "name": "Category"}),
        page._hide_panel,
    ):
        clear()
        assert not button.isVisible()
        page._selected_mod = mod
        page._show_mod_panel(mod)
        assert button.isVisible()


def test_click_rechecks_identity_before_revealing_the_deployment(preview, monkeypatch, qtbot):
    import subprocess

    page, mod, root = preview
    path = deploy(mod, root)
    page._refresh_panel_mod_status()
    button = deployment_button(page)
    opened = []
    monkeypatch.setattr(subprocess, "Popen", lambda args: opened.append(args))
    qtbot.mouseClick(button, Qt.MouseButton.LeftButton)
    assert opened == [["explorer", "/select,", str(path)]]
    write_deployment_identity(path, new_mod_id())
    qtbot.mouseClick(button, Qt.MouseButton.LeftButton)
    assert len(opened) == 1
    assert not button.isVisible()


@pytest.mark.parametrize("preview", [("zh-CN", "dark"), ("en-US", "light")], indirect=True)
def test_narrow_header_action_does_not_reflow_the_footer(preview, qtbot, qapp):
    page, mod, root = preview
    page._main_splitter.setSizes([600, 380])
    page._panel_splitter.setSizes([230, 340])
    variant_name = "Current: A very long variant name / 当前：很长的变体名称" * 3
    page._panel_variant_label.setText(variant_name)
    page._panel_variant_label.show()
    qtbot.wait(20)
    header = page.findChild(QWidget, "modPreviewHeader")
    before = header.height(), page._detail_tabs.y(), page._panel_preview.height()
    buttons = [page._panel_toggle_btn, page._panel_edit_btn, page._panel_open_btn, page._panel_delete_btn]
    footer_before = [item.mapTo(page._preview_panel, QPoint(0, 0)).y() for item in buttons]
    deploy(mod, root)
    page._refresh_panel_mod_status()
    qapp.processEvents()
    button = deployment_button(page)
    assert button.isVisible()
    assert (header.height(), page._detail_tabs.y(), page._panel_preview.height()) == before
    button_pos = button.mapTo(header, QPoint(0, 0))
    status_pos = page._panel_status_label.mapTo(header, QPoint(0, 0))
    variant_pos = page._panel_variant_label.mapTo(header, QPoint(0, 0))
    assert abs(button_pos.y() + button.height() // 2 - status_pos.y() - page._panel_status_label.height() // 2) <= 2
    assert variant_pos.x() + page._panel_variant_label.width() <= button_pos.x()
    assert button_pos.x() + button.width() <= header.width()
    assert page._panel_variant_label.toolTip() == variant_name
    rendered = page._panel_variant_label.text()
    assert rendered.startswith("Current:") and rendered != variant_name
    assert page._panel_variant_label.fontMetrics().horizontalAdvance(rendered) <= page._panel_variant_label.width()
    assert [item.mapTo(page._preview_panel, QPoint(0, 0)).y() for item in buttons] == footer_before
    mod.forget_deployment()
    page._refresh_panel_mod_status()
    qapp.processEvents()
    assert not button.isVisible()
    assert (header.height(), page._detail_tabs.y(), page._panel_preview.height()) == before
    assert [item.mapTo(page._preview_panel, QPoint(0, 0)).y() for item in buttons] == footer_before


def _show_mod_menu(page, mod_id, surface):
    """从真实卡片或表格右键入口显示指定 Mod 的菜单。"""
    if surface == "grid":
        page._show_mod_card_context_menu(page, mod_id, QPoint(10, 10))
    else:
        row = next(index for index, item in enumerate(page._display_items) if item["id"] == mod_id)
        pos = page._table.visualItemRect(page._table.item(row, 0)).center()
        page._show_table_context_menu(pos)


@pytest.mark.parametrize("surface", ["grid", "list"])
@pytest.mark.parametrize("state", ["active", "parked", "staging", "absent", "deploying"])
def test_context_deployment_action_matches_preview_availability(preview, monkeypatch, surface, state):
    import subprocess

    page, mod, root = preview
    path = None
    if state != "absent":
        name = {"parked": "DISABLED_Preview", "staging": "DISABLED_Preview.partial"}.get(state, "Preview")
        path = deploy(mod, root, name)
    if state == "active":
        mod.enable()
    if state == "deploying":
        page.begin_deploy_overlay(mod.mod_id)
    if surface == "list":
        page._set_view("list")
    page._refresh_panel_mod_status()
    available = state in ("active", "parked")
    assert deployment_button(page).isVisible() is available
    opened = []
    monkeypatch.setattr(subprocess, "Popen", lambda args: opened.append(args))

    def choose(menu):
        """检查菜单可用性与预览一致，仅在副本已发布时通过回车执行。"""
        action = next((action for action in menu.actions() if action.text() == "查看部署"), None)
        assert action is not None
        assert action.isEnabled() is available
        return action if available else None

    with interact(QMenu, choose):
        _show_mod_menu(page, mod.mod_id, surface)
    assert opened == ([["explorer", "/select,", str(path)]] if available else [])


@pytest.mark.parametrize("surface", ["grid", "list"])
@pytest.mark.parametrize("stale", [False, True])
def test_context_deployment_action_rechecks_clicked_mod_in_multi_selection(
    preview, mm, tmp_path, monkeypatch, surface, stale
):
    import subprocess

    page, mod, root = preview
    path = deploy(mod, root)
    other = _import_mod(mm, mod.node_id, tmp_path, "Other")
    page.set_current_mods([mod, other])
    if surface == "list":
        page._set_view("list")
    page._selected_ids = {mod.mod_id, other.mod_id}
    page._show_selection_panel()
    assert page._selected_mod is None
    opened = []
    monkeypatch.setattr(subprocess, "Popen", lambda args: opened.append(args))

    def choose(menu):
        """菜单打开后可替换部署身份，执行时必须重新核验。"""
        action = next((action for action in menu.actions() if action.text() == "查看部署"), None)
        assert action is not None and action.isEnabled()
        if stale:
            write_deployment_identity(path, new_mod_id())
        return action

    with interact(QMenu, choose):
        _show_mod_menu(page, mod.mod_id, surface)
    assert opened == ([] if stale else [["explorer", "/select,", str(path)]])
    assert page._selected_ids == {mod.mod_id, other.mod_id}
