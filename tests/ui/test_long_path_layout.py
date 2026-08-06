from __future__ import annotations

from types import SimpleNamespace

from PySide6.QtCore import QPoint, QRect

from src.core.ini_keys import KeyBinding
from src.models.mod import Mod
from src.ui.dialogs.mod_edit import ModEditDialog
from src.ui.pages.overview import OverviewPage
from src.ui.widgets.key_capture import KeyCaptureField


def _long_mod() -> Mod:
    path = "\\".join(
        [
            r"D:\Game\source\d3dxSkinManage-Plus\data\Example Game A\mods\performance",
            "2" * 32,
            "2" * 21,
            "0" * 16,
            "1" * 27,
            "3" * 27,
            "4" * 32,
            "Iota",
            ".d3dxmeta",
            "ini_overrides",
            "regional_iota_outfits",
        ]
    )
    return Mod(game_id="game_a", mod_id="long-path-mod", name="Iota", folder_name="Iota", path=path)


class _BindingManager:
    def __init__(self, binding: KeyBinding) -> None:
        self._binding = binding

    def parse_mod_key_bindings(self, _mod: Mod) -> list[KeyBinding]:
        return [self._binding]

    def effective_ini_disk_path(self, _mod: Mod, _rel: str) -> None:
        return None


def test_overview_long_path_keeps_key_button_in_view(qtbot, temp_data_dir):
    mod = _long_mod()
    binding = KeyBinding(
        ini_path=mod.path + r"\regional_iota_outfits.ini",
        section="KeySwap",
        key_line_no=1,
        key_value="h",
        rel="regional_iota_outfits.ini",
    )
    page = OverviewPage()
    qtbot.addWidget(page)

    try:
        page.set_mod_manager(_BindingManager(binding))
        page.resize(1000, 700)
        page.show()
        page._selected_mod = mod
        page._show_mod_panel(mod)
        page._detail_tabs.setCurrentWidget(page._panel_keys_scroll)
        qtbot.waitUntil(
            lambda: (
                page._panel_keys_container.findChild(KeyCaptureField) is not None
                and page._panel_keys_container.layout().count() > 0
            )
        )

        viewport = page._panel_keys_scroll.viewport()
        key_field = page._panel_keys_container.findChild(KeyCaptureField)
        assert key_field is not None

        button = key_field._btn
        left = button.mapTo(viewport, QPoint(0, 0)).x()
        right = button.mapTo(viewport, QPoint(button.width() - 1, 0)).x()

        assert page._panel_keys_host.width() <= viewport.width() + 2
        assert 0 <= left <= right < viewport.width()
        assert page._panel_path_label.text().endswith("regional_iota_outfits")
    finally:
        page.close()


def test_edit_dialog_long_path_stays_bounded(qtbot, temp_data_dir, monkeypatch):
    mod = _long_mod()
    dialog = ModEditDialog(mod)
    qtbot.addWidget(dialog)
    monkeypatch.setattr(dialog, "screen", lambda: SimpleNamespace(availableGeometry=lambda: QRect(0, 0, 1440, 900)))

    try:
        dialog.show()
        qtbot.waitUntil(dialog._name_input.hasFocus)
        initial_size = dialog.size()
        assert not dialog._path_label.isVisible()
        dialog._info_toggle.click()
        qtbot.waitUntil(lambda: dialog._path_label.verticalScrollBar().maximum() > 0)

        assert dialog.size() == initial_size
        assert 0 < dialog._path_label.width() < dialog.width()
        assert dialog._path_label.text() == mod.path
        assert dialog._path_label.document().firstBlock().layout().lineCount() >= 4
        assert dialog._path_label.document().size().height() > dialog._path_label.viewport().height()

        # 宽度改变可使列重新排列，但长路径和展开详情不得改变用户选择的高度。
        path_height = dialog._path_label.height()
        for width in (900, 450, 700):
            dialog.resize(width, initial_size.height())
            qtbot.wait(30)
            assert dialog.width() == width
            assert dialog.height() == initial_size.height()
            assert dialog._path_label.height() == path_height
            assert dialog._path_label.text() == mod.path
            assert dialog._form_scroll.horizontalScrollBar().maximum() == 0

            path_bottom = dialog._path_label.mapTo(dialog, QPoint(0, dialog._path_label.height())).y()
            created_top = dialog._created_label.mapTo(dialog, QPoint(0, 0)).y()
            assert path_bottom <= created_top
    finally:
        dialog.close()
