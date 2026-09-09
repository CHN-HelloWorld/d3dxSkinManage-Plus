import pytest
from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, Qt, QTimer
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import (
    QApplication,
    QColorDialog,
    QDialog,
    QFileDialog,
    QInputDialog,
    QLabel,
    QMessageBox,
    QProgressBar,
    QProgressDialog,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from src.ui import win_effects
from src.ui.dialogs.mod_updates_dialog import ModUpdatesDialog


@pytest.fixture
def chrome(qapp):
    previous = win_effects._dialog_titlebar_mgr
    native = qapp.testAttribute(Qt.ApplicationAttribute.AA_DontUseNativeDialogs)
    win_effects.install_dialog_titlebar_theming(qapp)
    yield
    manager = win_effects._dialog_titlebar_mgr
    if manager is not None and manager is not previous:
        qapp.removeEventFilter(manager)
        manager.deleteLater()
    win_effects._dialog_titlebar_mgr = previous
    qapp.setAttribute(Qt.ApplicationAttribute.AA_DontUseNativeDialogs, native)


def titlebar(dialog):
    bar = dialog.findChild(QLabel, "dialogWindowTitle")
    assert bar is not None, "Every dialog needs a visible custom window title"
    return bar


@pytest.mark.parametrize("kind", [QDialog, QMessageBox, QInputDialog, QProgressDialog])
@pytest.mark.parametrize("native_move_accepted", [False, True])
def test_titlebar_drags_when_native_move_does_not_take_over(
    chrome,
    qtbot,
    monkeypatch,
    kind,
    native_move_accepted,
):
    dialog = kind()
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.wait(10)
    bar = titlebar(dialog).parentWidget()
    origin = dialog.pos()
    point = QPointF(20, 16)
    global_point = QPointF(bar.mapToGlobal(point.toPoint()))

    def send(kind, global_pos, button, buttons):
        QApplication.sendEvent(
            bar, QMouseEvent(kind, point, global_pos, button, buttons, Qt.KeyboardModifier.NoModifier)
        )

    # 原生句柄随对话框销毁，实例方法必须在窗口仍存活时恢复。
    with monkeypatch.context() as move_patch:
        # 系统可能接受移动请求但未接管指针，继续送达标题栏的事件仍须移动窗口。
        move_patch.setattr(dialog.windowHandle(), "startSystemMove", lambda: native_move_accepted)
        send(QEvent.Type.MouseButtonPress, global_point, Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton)
        send(QEvent.Type.MouseMove, global_point + QPointF(63, 37), Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton)
        assert dialog.pos() == origin + QPoint(63, 37)
        send(
            QEvent.Type.MouseButtonRelease,
            global_point + QPointF(63, 37),
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.NoButton,
        )
        send(QEvent.Type.MouseMove, global_point + QPointF(90, 60), Qt.MouseButton.NoButton, Qt.MouseButton.NoButton)
        assert dialog.pos() == origin + QPoint(63, 37)


def test_update_window_shows_its_own_title_and_application_icon(qtbot):
    dialog = ModUpdatesDialog([])
    qtbot.addWidget(dialog)
    assert not dialog.windowIcon().isNull()
    assert dialog.windowTitle() == "更新 Mod"
    assert titlebar(dialog).text() == dialog.windowTitle()
    icon = dialog.findChild(QLabel, "dialogWindowIcon")
    assert icon is not None and not icon.pixmap().isNull()


@pytest.mark.parametrize("kind", [QDialog, QMessageBox, QInputDialog, QColorDialog])
def test_all_dialog_kinds_get_one_titlebar_without_covering_content(chrome, qtbot, tmp_path, kind):
    dialog = kind()
    qtbot.addWidget(dialog)
    dialog.setWindowTitle("窗口标题 <原样显示>")
    if kind is QDialog:
        QVBoxLayout(dialog).addWidget(QPushButton("内容"))
    elif kind is QMessageBox:
        dialog.setText("提示内容")
    elif kind is QInputDialog:
        dialog.setLabelText("请输入名称")
    elif kind is QFileDialog:
        dialog.setDirectory(str(tmp_path))
    dialog.show()
    qtbot.wait(10)
    assert dialog.windowFlags() & Qt.WindowType.FramelessWindowHint
    label = titlebar(dialog)
    assert label.text() == dialog.windowTitle()
    assert label.textFormat() == Qt.TextFormat.PlainText
    assert not dialog.windowIcon().isNull()
    bar = label.parentWidget()
    assert dialog.contentsRect().top() > bar.geometry().bottom()
    before = dialog.contentsMargins()
    dialog.hide()
    dialog.show()
    qtbot.wait(10)
    assert dialog.contentsMargins() == before
    assert len(dialog.findChildren(QLabel, "dialogWindowTitle")) == 1
    dialog.setWindowTitle("新标题")
    assert label.text() == "新标题"
    dialog.reject()


def test_static_confirmation_keeps_cancel_result_and_default_button(chrome, qtbot):
    seen = []

    def cancel():
        box = QApplication.activeModalWidget()
        seen.append(
            (
                box.windowFlags() & Qt.WindowType.FramelessWindowHint,
                titlebar(box).text(),
                box.standardButton(box.defaultButton()),
            )
        )
        box.button(QMessageBox.StandardButton.Cancel).click()

    QTimer.singleShot(50, cancel)
    result = QMessageBox.question(
        None,
        "保存更改",
        "是否保存？",
        QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
        QMessageBox.StandardButton.Cancel,
    )
    assert result == QMessageBox.StandardButton.Cancel
    # 静态消息框在返回时销毁，须在模态执行期间读取值。
    assert seen == [(Qt.WindowType.FramelessWindowHint, "保存更改", QMessageBox.StandardButton.Cancel)]


def test_custom_close_respects_veto_then_uses_reject_role(chrome, qtbot):
    class BusyDialog(QDialog):
        busy = True

        def closeEvent(self, event):
            if self.busy:
                event.ignore()
            else:
                super().closeEvent(event)

    dialog = BusyDialog()
    qtbot.addWidget(dialog)
    QVBoxLayout(dialog).addWidget(QLabel("任务运行中"))
    dialog.show()
    close = dialog.findChild(QPushButton, "dialogClose")
    assert close is not None
    close.click()
    assert dialog.isVisible()
    dialog.busy = False
    close.click()
    assert not dialog.isVisible()
    assert dialog.result() == QDialog.DialogCode.Rejected


def test_fixed_progress_window_keeps_content_space_and_disables_close(chrome, qtbot):
    dialog = QDialog()
    qtbot.addWidget(dialog)
    dialog.setFixedSize(460, 150)
    dialog.setWindowFlag(Qt.WindowType.WindowCloseButtonHint, False)
    QVBoxLayout(dialog).addWidget(QLabel("正在重建索引"))
    dialog.show()
    assert dialog.contentsRect().height() >= 150
    assert dialog.minimumSize() == dialog.maximumSize()
    close = dialog.findChild(QPushButton, "dialogClose")
    assert close is not None and not close.isEnabled()


def test_custom_message_close_selects_escape_button(chrome, qtbot):
    box = QMessageBox()
    qtbot.addWidget(box)
    box.setText("是否停止任务？")
    box.addButton("停止", QMessageBox.ButtonRole.DestructiveRole)
    keep = box.addButton("继续等待", QMessageBox.ButtonRole.RejectRole)
    box.setDefaultButton(keep)
    box.setEscapeButton(keep)
    box.show()
    close = box.findChild(QPushButton, "dialogClose")
    assert close is not None
    close.click()
    assert box.clickedButton() is keep


def test_update_dialog_does_not_get_a_second_header(chrome, qtbot):
    dialog = ModUpdatesDialog([])
    qtbot.addWidget(dialog)
    dialog.show()
    assert len(dialog.findChildren(QLabel, "dialogWindowTitle")) == 1
    assert dialog._header.y() == dialog._titlebar.geometry().bottom() + 1
    close = dialog._close_btn
    assert dialog.childAt(close.mapTo(dialog, close.rect().center())) is close


def test_shortcut_dialog_is_an_acrylic_window_instead_of_a_host_sized_overlay(chrome, qtbot):
    from src.ui.dialogs.shortcut_edit import _PrototypeDialog

    parent = QDialog()
    qtbot.addWidget(parent)
    parent.resize(1000, 800)
    parent.show()
    dialog = _PrototypeDialog(parent, "快捷键", "录制按键")
    qtbot.addWidget(dialog)
    dialog.show()
    assert dialog.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    assert dialog.width() < parent.width()
    assert titlebar(dialog).text() == "快捷键"
    assert dialog.close_button is dialog.findChild(QPushButton, "dialogClose")


def test_standard_caption_close_keeps_a_full_click_target(chrome, qtbot):
    dialog = QDialog()
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.wait(10)
    close = dialog.findChild(QPushButton, "dialogClose")
    assert close.width() == 42 and close.height() == 30


def test_progress_content_stays_below_caption_after_native_updates(chrome, qtbot):
    dialog = QProgressDialog("正在下载", "取消", 0, 100)
    qtbot.addWidget(dialog)
    dialog.setMinimumDuration(0)
    dialog.setAutoClose(False)
    dialog.show()
    canceled = []
    dialog.canceled.connect(lambda: canceled.append(True))
    for value in (1, 25, 70):
        dialog.setValue(value)
        dialog.setLabelText(f"下载进度 {value}%")
        dialog.resize(360, 180)
        qtbot.wait(10)
        label = next(label for label in dialog.findChildren(QLabel) if label.text().startswith("下载进度"))
        progress = dialog.findChild(QProgressBar)
        assert label.y() >= 32
        assert progress.y() > label.geometry().bottom()
        assert dialog.rect().contains(progress.geometry())
    cancel = next(b for b in dialog.findChildren(QPushButton) if b.text() == "取消")
    cancel.click()
    assert canceled == [True] and dialog.wasCanceled()


def test_new_dialog_surface_and_caption_share_translucent_material(chrome, qtbot, qapp):
    from src.ui import theme

    previous_qss, previous_theme = qapp.styleSheet(), theme.current_theme()
    try:
        for mode in ("light", "dark"):
            theme.set_theme(mode)
            qapp.setStyleSheet(theme.appearance_qss(mode))
            dialog = QDialog()
            qtbot.addWidget(dialog)
            dialog.resize(300, 200)
            dialog.show()
            qtbot.wait(10)
            pixels = dialog.grab().toImage()
            body = pixels.pixelColor(150, 150)
            caption = pixels.pixelColor(150, 10)
            assert 0 < body.alpha() < 255
            assert caption == body
            dialog.close()
    finally:
        theme.set_theme(previous_theme)
        qapp.setStyleSheet(previous_qss)


def test_idle_progress_does_not_repaint_continuously(chrome, qtbot):
    class PaintCounter(QObject):
        count = 0

        def eventFilter(self, obj, event):
            if event.type() == QEvent.Type.Paint:
                self.count += 1
            return False

    dialog = QProgressDialog("正在下载", "取消", 0, 100)
    qtbot.addWidget(dialog)
    dialog.setValue(10)
    dialog.show()
    counter = PaintCounter(dialog)
    dialog.installEventFilter(counter)
    qtbot.wait(100)
    counter.count = 0
    qtbot.wait(100)
    assert counter.count < 20


@pytest.mark.parametrize("close_method", ["accept", "reject", "caption"])
def test_nested_color_close_releases_modal_input_block(chrome, qtbot, close_method):
    host = QWidget()
    qtbot.addWidget(host)
    host_button = QPushButton("主窗口操作", host)
    QVBoxLayout(host).addWidget(host_button)
    host.show()
    editor = QDialog(host)
    editor_button = QPushButton("编辑窗口操作", editor)
    QVBoxLayout(editor).addWidget(editor_button)
    editor.open()
    qtbot.wait(30)
    color = QColorDialog(editor)
    color.setOption(QColorDialog.ColorDialogOption.DontUseNativeDialog)
    color.setWindowModality(Qt.WindowModality.WindowModal)
    color.open()
    qtbot.wait(30)
    assert QApplication.activeModalWidget() is color
    if close_method == "caption":
        button = color.findChild(QPushButton, "dialogClose")
        qtbot.mouseClick(
            color.windowHandle(), Qt.MouseButton.LeftButton, pos=button.mapTo(color, button.rect().center())
        )
    else:
        getattr(color, close_method)()
    qtbot.wait(50)
    assert not color.isVisible()
    assert QApplication.activeModalWidget() is editor
    clicked = []
    editor_button.clicked.connect(lambda: clicked.append("editor"))
    qtbot.mouseClick(
        editor.windowHandle(), Qt.MouseButton.LeftButton, pos=editor_button.mapTo(editor, editor_button.rect().center())
    )
    assert clicked == ["editor"]
    editor.close()
    qtbot.wait(30)
    assert QApplication.activeModalWidget() is None
    host_button.clicked.connect(lambda: clicked.append("host"))
    qtbot.mouseClick(
        host.windowHandle(), Qt.MouseButton.LeftButton, pos=host_button.mapTo(host, host_button.rect().center())
    )
    assert clicked == ["editor", "host"]


@pytest.mark.parametrize("close_method", ["accept", "reject", "caption"])
def test_tag_color_close_restores_real_editor_pointer_input(
    chrome,
    qtbot,
    qapp,
    temp_data_dir,
    close_method,
    monkeypatch,
):
    from src.models.mod import Mod, ModInfo
    from src.ui import anim, fonts, theme, win_dnd_admin
    from src.ui.background_style import BackgroundStyle
    from src.ui.dialogs.mod_edit import ModEditDialog

    old_style = qapp.styleSheet()
    monkeypatch.setattr(anim, "_enabled", True)
    qapp.setStyleSheet(theme.appearance_qss("dark"))
    host = QWidget()
    qtbot.addWidget(host)
    host.show()
    hook = win_dnd_admin._ArmOnShow(qapp) if qapp.platformName() == "windows" else None
    if hook:
        qapp.installEventFilter(hook)
    font_hook = fonts._FontInheritance(qapp)
    qapp.installEventFilter(font_hook)
    surfaces = BackgroundStyle(qapp)
    surfaces.apply(True)
    editor = ModEditDialog(Mod(name="Example", info=ModInfo(tags=["First", "Second"])), host)
    editor.setModal(True)
    try:
        editor.show()
        qtbot.wait(150)
        picker = editor.tag_picker
        tag_id = picker.draft.selected_ids[0]
        picker.open_editor(tag_id)
        qtbot.wait(50)
        picker.editor_card.custom_button.click()
        color = picker.editor_card.color_dialog
        qtbot.wait(100)
        assert color.isVisible()
        if close_method == "caption":
            button = color.findChild(QPushButton, "dialogClose")
            qtbot.mouseClick(
                color.windowHandle(), Qt.MouseButton.LeftButton, pos=button.mapTo(color, button.rect().center())
            )
        else:
            getattr(color, close_method)()
        # 颜色对话框使用统一的透明表面关闭动画。
        qtbot.waitUntil(lambda: not color.isVisible())
        assert QApplication.activeModalWidget() is editor
        clicked = []
        editor._save_button.clicked.connect(lambda: clicked.append(True))
        button = editor._save_button
        qtbot.wait(50)
        assert editor.childAt(button.mapTo(editor, button.rect().center())) is button
        point = button.mapTo(editor, button.rect().center())
        target = editor.childAt(point)
        native_enabled = None
        if qapp.platformName() == "windows":
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.WinDLL("user32")
            user32.IsWindowEnabled.argtypes = [wintypes.HWND]
            hwnd = int(editor.effectiveWinId())
            native_enabled = bool(user32.IsWindowEnabled(hwnd))
            # 在本窗口内执行原生命中测试，避免其他桌面窗口遮挡。
            # 不可跳过透明子窗口，系统仍可能将点击发送到提升后的透明框架。
            user32.ChildWindowFromPointEx.argtypes = [wintypes.HWND, wintypes.POINT, wintypes.UINT]
            user32.ChildWindowFromPointEx.restype = wintypes.HWND
            user32.MapWindowPoints.argtypes = [
                wintypes.HWND,
                wintypes.HWND,
                ctypes.POINTER(wintypes.POINT),
                wintypes.UINT,
            ]
            user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t]
            user32.SendMessageW.restype = ctypes.c_ssize_t
            native_point = wintypes.POINT(
                round(point.x() * editor.devicePixelRatioF()), round(point.y() * editor.devicePixelRatioF())
            )
            while True:
                child = user32.ChildWindowFromPointEx(hwnd, native_point, 1 | 2)
                if not child or child == hwnd:
                    break
                user32.MapWindowPoints(hwnd, child, ctypes.byref(native_point), 1)
                hwnd = child
            target = QWidget.find(hwnd)
            packed = ((native_point.y & 0xFFFF) << 16) | (native_point.x & 0xFFFF)
            user32.SendMessageW(hwnd, 0x0200, 0, packed)
            user32.SendMessageW(hwnd, 0x0201, 1, packed)
            qtbot.wait(30)
            user32.SendMessageW(hwnd, 0x0202, 0, packed)
            qtbot.wait(50)
        else:
            qtbot.mouseClick(editor.windowHandle(), Qt.MouseButton.LeftButton, pos=point)
        assert clicked, dict(
            visible=editor.isVisible(),
            enabled=editor.isEnabled(),
            native_enabled=native_enabled,
            target=repr(target),
            button=repr(button),
            point=(point.x(), point.y()),
            active=editor.isActiveWindow(),
            grabber=repr(QWidget.mouseGrabber()),
            modal=repr(QApplication.activeModalWidget()),
        )
    finally:
        editor.done(QDialog.DialogCode.Rejected)
        qtbot.wait(200)
        if hook:
            qapp.removeEventFilter(hook)
        qapp.removeEventFilter(font_hook)
        surfaces.apply(False)
        surfaces.dispose()
        qapp.setStyleSheet(old_style)


@pytest.mark.parametrize("kind", [QDialog, QMessageBox, QInputDialog, QColorDialog, QProgressDialog])
def test_global_dialog_material_is_prepared_before_show_and_reused(chrome, qtbot, kind):
    from src.ui.acrylic import _DialogAcrylicSurface

    dialog = kind()
    qtbot.addWidget(dialog)
    dialog.ensurePolished()
    assert dialog.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    assert not dialog.findChildren(_DialogAcrylicSurface)
    dialog.show()
    surfaces = dialog.findChildren(_DialogAcrylicSurface)
    assert len(surfaces) == 1 and surfaces[0].parent() is dialog
    assert surfaces[0]._alpha == pytest.approx(0.78)
    hwnd = int(dialog.winId())
    dialog.hide()
    dialog.show()
    assert int(dialog.winId()) == hwnd
    assert dialog.findChildren(_DialogAcrylicSurface) == surfaces
    dialog.reject()
    assert not dialog.isVisible()


def test_file_picker_retains_native_dialog_eligibility(chrome, qtbot, qapp):
    dialog = QFileDialog()
    qtbot.addWidget(dialog)
    dialog.ensurePolished()
    assert not qapp.testAttribute(Qt.ApplicationAttribute.AA_DontUseNativeDialogs)
    assert not dialog.testOption(QFileDialog.Option.DontUseNativeDialog)
    assert not dialog.windowFlags() & Qt.WindowType.FramelessWindowHint
    assert not dialog.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    assert not dialog.property("dialogFrameInstalled")


@pytest.mark.parametrize("kind", ["shortcut", "updates", "batch"])
def test_custom_dialog_content_preserves_surface_material(chrome, qtbot, qapp, kind):
    from src.ui import theme
    from src.ui.acrylic import _DialogAcrylicSurface
    from src.ui.dialogs.shortcut_edit import _PrototypeDialog
    from src.ui.pages.overview import BatchAddCharacterDialog

    old_style = qapp.styleSheet()
    qapp.setStyleSheet(theme.appearance_qss("dark"))
    try:
        host = QWidget()
        qtbot.addWidget(host)
        if kind == "shortcut":
            dialog = _PrototypeDialog(host, "Shortcuts", "Record a key")
        elif kind == "updates":
            dialog = ModUpdatesDialog([])
        else:
            dialog = BatchAddCharacterDialog(host)
        dialog._test_host = host
        qtbot.addWidget(dialog)
        dialog.show()
        qtbot.wait(10)
        surfaces = dialog.findChildren(_DialogAcrylicSurface)
        if kind == "updates":
            assert not surfaces
            assert not dialog.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        else:
            assert len(surfaces) == 1 and surfaces[0].parent() is dialog
        pixels = dialog.grab().toImage()
        if kind == "shortcut":
            point = dialog.content.mapTo(dialog, dialog.content.rect().topLeft())
        elif kind == "updates":
            point = dialog._header.pos()
        else:
            point = dialog.contentsRect().topLeft()
        alpha = pixels.pixelColor(
            round((point.x() + 5) * dialog.devicePixelRatioF()), round((point.y() + 5) * dialog.devicePixelRatioF())
        ).alpha()
        assert alpha == 255 if kind == "updates" else 0 < alpha < 255
        dialog.close()
    finally:
        qapp.setStyleSheet(old_style)
