from __future__ import annotations

import pytest
from PySide6.QtCore import Qt

from src.ui.widgets.key_capture import KeyCaptureField, qt_key_to_token
from tests.helpers import registered_games as registered_games


def test_qt_key_to_token_letter():
    assert qt_key_to_token(Qt.Key.Key_H, Qt.KeyboardModifier.NoModifier) == "h"


def test_qt_key_to_token_function_key():
    assert qt_key_to_token(Qt.Key.Key_F10, Qt.KeyboardModifier.NoModifier) == "VK_F10"


def test_qt_key_to_token_arrow():
    assert qt_key_to_token(Qt.Key.Key_Down, Qt.KeyboardModifier.NoModifier) == "VK_DOWN"


def test_qt_key_to_token_with_modifiers():
    mods = Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier
    assert qt_key_to_token(Qt.Key.Key_X, mods) == "ctrl alt x"


def test_field_shows_friendly_name(qtbot):
    f = KeyCaptureField("VK_DOWN")
    qtbot.addWidget(f)
    assert "方向键下" in f.display_text()
    assert f.token() == "VK_DOWN"


def test_field_capture_emits_token_changed(qtbot):
    f = KeyCaptureField("y")
    qtbot.addWidget(f)
    with qtbot.waitSignal(f.token_changed, timeout=1000) as blocker:
        f.enter_capture()
        qtbot.keyClick(f, Qt.Key.Key_H)
    assert blocker.args == ["h"]
    assert f.token() == "h"


def test_field_escape_cancels_capture(qtbot):
    f = KeyCaptureField("y")
    qtbot.addWidget(f)
    f.enter_capture()
    f._handle_captured(Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier)
    assert f.token() == "y"  # 未变
    assert not f._capturing


def test_capture_collects_a_chord_until_release(qtbot):
    field = KeyCaptureField("y")
    qtbot.addWidget(field)
    field.enter_capture()
    qtbot.keyPress(field, Qt.Key.Key_K, Qt.KeyboardModifier.ControlModifier)
    assert field.token() == "y"
    qtbot.keyPress(field, Qt.Key.Key_J, Qt.KeyboardModifier.ControlModifier)
    qtbot.keyRelease(field, Qt.Key.Key_J, Qt.KeyboardModifier.ControlModifier)
    assert field.token() == "ctrl k j"
    assert not field._capturing


@pytest.mark.parametrize(
    "key, modifiers, expected",
    [
        (Qt.Key.Key_1, Qt.KeyboardModifier.KeypadModifier, "VK_NUMPAD1"),
        (Qt.Key.Key_Plus, Qt.KeyboardModifier.KeypadModifier, "VK_ADD"),
        (Qt.Key.Key_Slash, Qt.KeyboardModifier.KeypadModifier, "VK_DIVIDE"),
        (Qt.Key.Key_X, Qt.KeyboardModifier.MetaModifier, "VK_LWIN x"),
        (Qt.Key.Key_CapsLock, Qt.KeyboardModifier.NoModifier, "VK_CAPITAL"),
    ],
)
def test_capture_extended_windows_keys(key, modifiers, expected):
    assert qt_key_to_token(key, modifiers) == expected


@pytest.mark.parametrize(
    "modifiers, expected",
    [
        (Qt.KeyboardModifier.NoModifier, "VK_TAB"),
        (Qt.KeyboardModifier.ShiftModifier, "shift VK_TAB"),
    ],
)
def test_tab_is_captured_without_focus_traversal(qtbot, modifiers, expected):
    field = KeyCaptureField("x")
    qtbot.addWidget(field)
    field.show()
    field.enter_capture()
    qtbot.keyClick(field, Qt.Key.Key_Tab, modifiers)
    assert field.token() == expected


def test_modifier_pressed_after_primary_key_is_included(qtbot):
    field = KeyCaptureField("x")
    qtbot.addWidget(field)
    field.enter_capture()
    qtbot.keyPress(field, Qt.Key.Key_K)
    qtbot.keyPress(field, Qt.Key.Key_Control)
    qtbot.keyRelease(field, Qt.Key.Key_K, Qt.KeyboardModifier.ControlModifier)
    assert field.token() == "ctrl k"


@pytest.mark.parametrize("text, expected", [("Ctrl+Alt+X", "Ctrl Alt X"), ("+", "+"), ("ctrl +", "ctrl +")])
def test_manual_chords_preserve_plus_key_alias(qtbot, monkeypatch, text, expected):
    from PySide6.QtWidgets import QInputDialog

    field = KeyCaptureField("x")
    qtbot.addWidget(field)
    monkeypatch.setattr(QInputDialog, "getText", lambda *args, **kwargs: (text, True))
    field._enter_manual()
    assert field.token() == expected


# 按键面板改走容器模型——_build_keys_panel 无参（读 self._selected_mod），
# 解析经 mod_manager.parse_mod_key_bindings（override 优先、归档也能读），编辑经
# edit_mod_key 写**容器 override**（本体零改动）。以下三个 UI 测试按新架构重写：
# 用真 ModManager 建容器 Mod、注入 page、选中后驱动面板/编辑，断言 override 行为。


def _make_container_mod(mm, char_id, name, ini_text):
    """在 mm 库内造一个文件夹本体容器 Mod 并登记，返回 Mod。"""
    from pathlib import Path

    from src.core.mod_package import write_container_marker
    from src.models.mod import Mod, ModStatus

    lib = mm._get_character_library(char_id)
    container = Path(lib) / name
    body = container / name
    body.mkdir(parents=True)
    (body / "merged.ini").write_bytes(ini_text.encode("utf-8"))
    content_hash = Mod.compute_sha1(str(body))
    from src.core.mod_identity import new_mod_id

    mod_id = new_mod_id()
    write_container_marker(container, name, content_hash, mod_id=mod_id, imported_at="2026-10-05T10:00:00")
    mod = Mod(
        game_id="game_a",
        imported_at="2026-10-05T10:00:00",
        mod_id=mod_id,
        name=name,
        folder_name=name,
        path=str(body),
        content_hash=content_hash,
        node_id=char_id,
        status=ModStatus.DISABLED,
    )
    mm._index.add_mod(mod)
    assert mm._write_mod_sidecar(mod)
    return mod


def test_overview_builds_editable_key_panel(qtbot, temp_data_dir):
    from PySide6.QtWidgets import QGridLayout, QWidget

    from src.core.mod_manager import ModManager
    from src.ui.pages.overview import OverviewPage

    mm = ModManager()
    mm.set_game("game_a")
    char = mm.add_character(name="示例角色戊", node_type="character")
    mod = _make_container_mod(
        mm,
        char.id,
        "KeyMod",
        "[KeySwap]\nkey = y\nback = VK_DOWN\ntype = cycle\n",
    )

    page = OverviewPage()
    qtbot.addWidget(page)
    page.set_mod_manager(mm)
    page._selected_mod = mod
    page._panel_mode = "mod"
    page._build_keys_panel()
    qtbot.waitUntil(
        lambda: len(page._panel_keys_container.findChildren(KeyCaptureField)) == 2,
        timeout=1000,
    )

    fields = page._panel_keys_container.findChildren(KeyCaptureField)
    assert len(fields) == 2  # 前进和后退快捷键。
    tokens = {f.token() for f in fields}
    assert tokens == {"y", "VK_DOWN"}

    # 前进和后退快捷键位于同一网格行的两列。
    binding_row = page._panel_keys_container.findChild(QWidget, "iniBindingRow")
    assert binding_row is not None
    grid = binding_row.layout()
    assert isinstance(grid, QGridLayout)
    positions = {field.property("bindingRole"): grid.getItemPosition(grid.indexOf(field)) for field in fields}
    assert positions["forward"][0] == positions["back"][0]
    assert 0 < positions["forward"][1] < positions["back"][1]


def test_overview_archive_multi_ini_shows_open_button_for_each_file(
    qtbot,
    temp_data_dir,
    tmp_path,
):
    from PySide6.QtCore import QPoint
    from PySide6.QtWidgets import QPushButton

    from src.core.ini_keys import KeyBinding
    from src.models.mod import Mod
    from src.ui.pages.overview import OverviewPage

    archive = tmp_path / "multi_ini.7z"
    archive.write_bytes(b"archive-placeholder")
    open_paths = {}
    bindings = []
    rels = (
        "outfit/main.ini",
        "weapon/avatarfix_multi_ini_with_an_extremely_long_file_name_that_must_elide.ini",
    )
    for index, rel in enumerate(rels):
        target = tmp_path / f"effective_{index}.ini"
        target.write_text("[KeySwap]\nkey = h\n", encoding="utf-8")
        open_paths[rel] = str(target)
        bindings.append(
            KeyBinding(
                ini_path=rel,
                section="KeySwap",
                key_line_no=1,
                key_value="h",
                rel=rel,
            )
        )

    class _ArchiveBindingManager:
        def parse_mod_key_bindings(self, _mod):
            return bindings

        def effective_ini_disk_path(self, _mod, rel):
            return open_paths.get(rel)

    page = OverviewPage()
    qtbot.addWidget(page)
    page.set_mod_manager(_ArchiveBindingManager())
    page._selected_mod = Mod(
        mod_id="archive-multi-ini",
        game_id="game_a",
        imported_at="2026-10-05T10:00:00",
        name="Multi INI",
        folder_name="Multi INI",
        path=str(archive),
    )
    page._show_mod_panel(page._selected_mod)
    qtbot.waitUntil(
        lambda: (
            len(
                [
                    button
                    for button in page._panel_keys_container.findChildren(QPushButton)
                    if button.objectName() == "iniOpenFileButton"
                ]
            )
            == 2
        ),
        timeout=1000,
    )

    open_buttons = [
        button
        for button in page._panel_keys_container.findChildren(QPushButton)
        if button.objectName() == "iniOpenFileButton"
    ]
    assert len(open_buttons) == 2
    assert all(button.parentWidget().objectName() == "iniFileHeader" for button in open_buttons)

    page.resize(1000, 700)
    page.show()
    qtbot.wait(20)
    page._detail_tabs.setCurrentIndex(1)
    qtbot.wait(10)
    viewport = page._panel_keys_scroll.viewport()
    for button in open_buttons:
        left = button.mapTo(viewport, QPoint(0, 0)).x()
        right = button.mapTo(viewport, QPoint(button.width() - 1, 0)).x()
        assert 0 <= left <= right < viewport.width()


def test_overview_empty_keys_hides_panel(qtbot, temp_data_dir):
    from src.core.mod_manager import ModManager
    from src.ui.pages.overview import OverviewPage

    mm = ModManager()
    mm.set_game("game_a")
    char = mm.add_character(name="示例角色戊", node_type="character")
    # 无 ini 的容器 Mod → 面板应隐藏
    mod = _make_container_mod(mm, char.id, "NoKeyMod", "")
    (Path_of(mod) / "merged.ini").unlink()  # 删掉 ini，留空本体

    page = OverviewPage()
    qtbot.addWidget(page)
    page.set_mod_manager(mm)
    page._selected_mod = mod
    page._build_keys_panel()
    assert page._panel_keys_container.isVisibleTo(page) is False


def test_overview_refresh_keys_only_for_selected_mod(qtbot, temp_data_dir, monkeypatch):
    from src.core.mod_manager import ModManager
    from src.ui.pages.overview import OverviewPage

    mm = ModManager()
    mm.set_game("game_a")
    char = mm.add_character(name="示例角色戊", node_type="character")
    mod = _make_container_mod(mm, char.id, "SelectedKeyMod", "[KeySwap]\nkey = y\n")

    page = OverviewPage()
    qtbot.addWidget(page)
    page.set_mod_manager(mm)
    page._selected_mod = mod
    calls: list[bool] = []
    monkeypatch.setattr(page, "_build_keys_panel", lambda: calls.append(True))

    assert page.refresh_keys_if_selected("another_mod") is False
    assert calls == []
    assert page.refresh_keys_if_selected(mod.mod_id) is True
    assert calls == [True]


def test_overview_key_change_writes_override_not_body(qtbot, temp_data_dir):
    from src.core.mod_manager import ModManager
    from src.core.mod_package import read_override, resolve_effective_ini_text
    from src.ui.pages.overview import OverviewPage

    mm = ModManager()
    mm.set_game("game_a")
    char = mm.add_character(name="示例角色戊", node_type="character")
    mod = _make_container_mod(mm, char.id, "EditMod", "[KeySwap]\nkey = y\n")
    body_ini = Path_of(mod) / "merged.ini"

    page = OverviewPage()
    qtbot.addWidget(page)
    page.set_mod_manager(mm)
    page._selected_mod = mod
    page._build_keys_panel()

    binding = mm.parse_mod_key_bindings(mod)[0]
    page._on_key_changed(binding, "key", "h")  # 模拟捕获后写回

    # 本体原始 ini 不变（改动在 override）
    assert body_ini.read_bytes() == b"[KeySwap]\nkey = y\n"
    # override 存在且含新键，有效 ini 反映改动
    ov = read_override(mod.path, "merged.ini")
    assert ov is not None and "key = h" in ov
    assert "key = h" in resolve_effective_ini_text(mod.path, "merged.ini")


def Path_of(mod):
    from pathlib import Path

    return Path(mod.path)
