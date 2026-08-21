from types import SimpleNamespace

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QDialog, QFrame, QMenu, QVBoxLayout, QWidget

from src.ui import acrylic, icons, theme
from src.ui.dialog_chrome import decorate_dialog


@pytest.fixture
def appearance(qapp, monkeypatch):
    previous = qapp.styleSheet(), theme.current_theme(), theme.is_background_active()
    monkeypatch.setattr(icons, "write_themed_svg", lambda *args, **kwargs: None)
    popup_filter = acrylic.AcrylicPopupFilter()
    qapp.installEventFilter(popup_filter)

    def apply(mode, background):
        theme.set_theme(mode)
        theme.set_background_active(background)
        qapp.setStyleSheet(theme.appearance_qss(mode))
        acrylic.reset_for_theme_change()

    yield apply
    qapp.removeEventFilter(popup_filter)
    theme.set_theme(previous[1])
    theme.set_background_active(previous[2])
    qapp.setStyleSheet(previous[0])


def pixel(widget, x, y):
    image = widget.grab().toImage()
    dpr = widget.devicePixelRatioF()
    return image.pixelColor(round(x * dpr), round(y * dpr))


def test_context_menu_keeps_readable_acrylic_across_background_changes(qtbot, appearance):
    owner = QWidget()
    qtbot.addWidget(owner)
    owner.setStyleSheet("background: #fff0bc; color: #ff0000;")
    menu = QMenu(owner)
    menu.addAction("Menu action")
    for mode, background in (("light", True), ("dark", True), ("light", False), ("light", True)):
        appearance(mode, background)
        menu.show()
        assert menu.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        assert 190 < pixel(menu, menu.width() // 2, 3).alpha() < 250
        menu.hide()


@pytest.mark.parametrize("mode", ["light", "dark"])
def test_custom_caption_does_not_cover_opted_in_dialog_acrylic(qtbot, appearance, mode):
    appearance(mode, True)
    dialog = QDialog()
    qtbot.addWidget(dialog)
    dialog.resize(320, 220)
    acrylic.apply_dialog_acrylic(dialog, tint_alpha=0.65)
    decorate_dialog(dialog)
    dialog.show()
    # 在内容空白和标题栏空白位置采样，避开文本及控件。
    assert 100 < pixel(dialog, 160, 150).alpha() < 250
    assert 100 < pixel(dialog, 220, 15).alpha() < 250
    assert dialog.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)


@pytest.mark.parametrize("mode", ["light", "dark"])
def test_tag_filter_has_frosted_surface_and_still_dismisses(qtbot, appearance, mode):
    from src.ui.widgets.tag_filter import TagFilter

    appearance(mode, True)
    owner = QWidget()
    qtbot.addWidget(owner)
    owner.show()
    tags = TagFilter(owner)
    tags.button.click()
    popup = tags._popup
    assert 100 < pixel(popup, popup.width() // 2, 4).alpha() < 250
    qtbot.keyClick(popup, Qt.Key.Key_Escape)
    assert not popup.isVisible()
    assert not tags.button.isChecked()


def test_tag_color_dialog_preserves_preview_color_on_frosted_shell(qtbot, appearance):
    from src.ui.widgets.tag_editor import TagEditorCard

    appearance("light", True)
    owner = QDialog()
    qtbot.addWidget(owner)
    editor = TagEditorCard(owner)
    QVBoxLayout(owner).addWidget(editor)
    editor.hex_input.setText("#12ABEF")
    owner.show()
    editor.custom_button.click()
    dialog = editor.color_dialog
    decorate_dialog(dialog)
    assert dialog.currentColor() == QColor("#12ABEF")
    assert 100 < pixel(dialog, dialog.width() // 2, 3).alpha() < 250
    preview = next(w for w in dialog.findChildren(QWidget) if w.metaObject().className().endswith("QColorShowLabel"))
    assert pixel(preview, preview.width() // 2, preview.height() // 2) == QColor("#12ABEF")
    selected = []
    editor.color_chosen.connect(selected.append)
    dialog.accept()
    assert selected == ["#12ABEF"]


def test_shortcut_panels_transmit_background_only_when_enabled(qtbot, appearance, temp_data_dir):
    from src.ui.pages.shortcuts import ShortcutsPage
    from src.ui.shortcut_manager import ShortcutSettings

    page = ShortcutsPage(ShortcutSettings())
    qtbot.addWidget(page)
    page.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    page.resize(1000, 800)
    for mode, background in (("light", True), ("dark", True), ("light", False)):
        appearance(mode, background)
        page.retheme()
        page.show()
        group = page.findChild(QFrame, "shortcutGroup")
        alpha = pixel(group, group.width() // 2, 3).alpha()
        assert 150 < alpha < 250 if background else alpha == 255


def test_combo_popup_prepares_surface_before_show(qtbot, appearance):
    from PySide6.QtWidgets import QComboBox

    appearance("light", True)
    combo = QComboBox()
    qtbot.addWidget(combo)
    combo.addItems(["First", "Second"])
    container = combo.view().window()
    container.ensurePolished()
    assert not container.isVisible()
    assert not container.testAttribute(Qt.WidgetAttribute.WA_WState_Created)
    assert container.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    combo.show()
    combo.showPopup()
    qtbot.waitUntil(container.isVisible)
    assert container.testAttribute(Qt.WidgetAttribute.WA_WState_Created)
    combo.view().setCurrentIndex(combo.model().index(1, 0))
    qtbot.keyClick(combo.view(), Qt.Key.Key_Return)
    assert combo.currentIndex() == 1 and not container.isVisible()


@pytest.mark.parametrize("sampled", [False, True])
@pytest.mark.parametrize("editable", [False, True])
def test_combo_popup_keeps_one_tint_and_handle_after_retheme(qtbot, appearance, monkeypatch, editable, sampled):
    """两种合成路径都只叠加一次主题底色，重开缓存窗口后仍使用同一句柄。"""
    from PySide6.QtWidgets import QComboBox

    from src.ui import win_effects

    monkeypatch.setattr(
        win_effects,
        "sys",
        SimpleNamespace(platform="win32", getwindowsversion=lambda: SimpleNamespace(build=19045 if sampled else 22631)),
    )
    owner = QWidget()
    qtbot.addWidget(owner)
    owner.setObjectName("comboTintOwner")
    owner.setStyleSheet("QWidget#comboTintOwner { background: #2070c0; }")
    owner.resize(420, 300)
    combo = QComboBox(owner)
    combo.setEditable(editable)
    combo.addItems(["First", "Second", "Third"])
    combo.setGeometry(30, 30, 240, 32)
    container = combo.view().window()
    handle = None
    for mode, background in (("light", False), ("dark", True), ("light", True)):
        appearance(mode, background)
        owner.show()
        combo.showPopup()

        def assert_single_tint():
            """按独立算出的叠色结果检查实际像素，不能用固定不透明底色代替背景。"""
            color = pixel(container, container.width() - 30, container.height() - 14)
            if sampled:
                assert color.alpha() == 255
                expected = (242, 246, 251) if mode == "light" else (42, 47, 52)
            else:
                assert 210 <= color.alpha() <= 218
                expected = (255, 255, 255) if mode == "light" else (43, 43, 43)
            assert all(abs(actual - wanted) <= 2 for actual, wanted in zip(color.getRgb()[:3], expected))

        qtbot.waitUntil(assert_single_tint)
        current_handle = int(container.winId())
        assert handle is None or current_handle == handle
        handle = current_handle
        combo.hidePopup()


def test_popup_install_disables_qt_snapshot_effects(qapp):
    acrylic.install(qapp)
    assert not qapp.isEffectEnabled(Qt.UIEffect.UI_AnimateCombo)
    assert not qapp.isEffectEnabled(Qt.UIEffect.UI_FadeTooltip)


@pytest.mark.parametrize("mode", ["light", "dark"])
@pytest.mark.parametrize("build, alpha_range", [(19045, (235, 245)), (22631, (195, 203))])
def test_dialog_tint_is_uniform_under_children_and_resize_corners(
    qtbot, appearance, monkeypatch, mode, build, alpha_range
):
    from PySide6.QtWidgets import QLabel

    from src.ui import win_effects

    monkeypatch.setattr(
        win_effects,
        "sys",
        SimpleNamespace(platform="win32", getwindowsversion=lambda: SimpleNamespace(build=build)),
    )
    appearance(mode, False)
    dialog = QDialog()
    qtbot.addWidget(dialog)
    dialog.resize(320, 180)
    layout = QVBoxLayout(dialog)
    label = QLabel("Readable message")
    layout.addWidget(label)
    acrylic.apply_dialog_acrylic(dialog, tint_alpha=0.78, animate=False)
    frame = decorate_dialog(dialog)
    dialog.show()
    qtbot.wait(10)
    # 强制透明子控件下方重复传播背景。
    for _ in range(3):
        label.repaint()
        for handle in frame.handles:
            handle.repaint()
    samples = [
        pixel(dialog, 8, dialog.height() - 8),
        pixel(dialog, dialog.width() - 9, dialog.height() - 9),
        pixel(dialog, 16, dialog.height() - 20),
        pixel(dialog, 240, 60),
    ]
    assert all(alpha_range[0] <= color.alpha() <= alpha_range[1] for color in samples)
    dialog.close()
