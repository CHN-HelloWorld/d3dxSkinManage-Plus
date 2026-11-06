"""验证 Mod 卡片排序抓手、鼠标判定和控件池复用不会影响原有交互。"""

import pytest
from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, Qt, QTimer
from PySide6.QtGui import QContextMenuEvent, QMouseEvent, QPixmap
from PySide6.QtWidgets import QApplication, QMenu

from src.models.mod import Mod, ModInfo
from src.ui import icons, plugin_menu
from src.ui.widgets import mod_card
from src.ui.widgets.mod_card import ModCard


@pytest.fixture
def card(qtbot):
    """创建可接收鼠标事件且由 Qt 测试夹具清理的已绑定卡片。"""
    widget = ModCard(Mod(mod_id="mod-one", name="示例 Mod", info=ModInfo(tags=["Shared"])))
    qtbot.addWidget(widget)
    widget.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
    widget.show()
    return widget


def _move(card, position, buttons=Qt.MouseButton.LeftButton):
    """投递带明确按键状态的移动事件，不依赖平台鼠标抓取。"""
    event = QMouseEvent(
        QEvent.Type.MouseMove,
        QPointF(position),
        QPointF(card.mapToGlobal(position)),
        Qt.MouseButton.NoButton,
        buttons,
        Qt.KeyboardModifier.NoModifier,
    )
    QApplication.sendEvent(card, event)


@pytest.mark.parametrize("visible", [True, False])
def test_gallery_card_is_not_draggable_by_default(card, qtbot, visible):
    """图库和隐藏卡片默认不因长按或移动发出排序请求。"""
    requests = []
    card.reorder_requested.connect(requests.append)
    if not visible:
        card.hide()
    position = QPoint(36, 36)
    qtbot.mousePress(card, Qt.MouseButton.LeftButton, pos=position)
    _move(card, position + QPoint(QApplication.startDragDistance(), 0))
    qtbot.wait(240)
    qtbot.mouseRelease(card, Qt.MouseButton.LeftButton)
    assert requests == []
    assert card._reorder_grip.isHidden()
    assert not card._press_timer.isActive()


def test_reorder_grip_is_translated_and_uses_top_right_overlay(card, monkeypatch):
    """启用排序显示主题六点抓手及翻译提示，关闭后清除提示。"""
    monkeypatch.setattr(mod_card, "tr", lambda key: f"translated:{key}")
    card.set_reorder_enabled(True)
    assert card._reorder_grip.isVisible()
    assert card._reorder_grip.geometry() == QRect(172, 4, 24, 24)
    assert card._reorder_grip.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
    assert card._reorder_grip.alignment() == Qt.AlignmentFlag.AlignCenter
    assert card._reorder_grip.styleSheet() == "background-color: rgba(0,0,0,0.35); border-radius: 4px;"
    assert card._reorder_grip.pixmap().toImage() == icons.pixmap("actions/reorder", 16, "text.on_accent").toImage()
    assert card.toolTip() == "translated:长按并拖动 Mod 卡片调整顺序，放下后自动保存。"
    card.set_reorder_enabled(False)
    assert card._reorder_grip.isHidden()
    assert card.toolTip() == ""


@pytest.mark.parametrize("resize_method", ["resize_card", "resize_card_fast"])
@pytest.mark.parametrize("width,height", [(80, 100), (160, 200), (320, 400)])
def test_resize_keeps_reorder_grip_at_top_right(card, resize_method, width, height):
    """两条尺寸更新路径都保持抓手大小和右上角间距。"""
    card.set_reorder_enabled(True)
    getattr(card, resize_method)(width, height)
    assert card._reorder_grip.geometry() == QRect(width - 28, 4, 24, 24)
    assert card._reorder_grip.isVisible()


def test_reorder_visibility_invalidates_scroll_render_cache(card):
    """抓手显示状态改变时不能复用缺少抓手的滚动截图。"""
    original = card.scroll_render_key()
    assert original is not None
    card.set_reorder_enabled(True)
    assert card.scroll_render_key() != original
    card.set_reorder_enabled(False)
    assert card.scroll_render_key() == original


def test_long_press_emits_card_once_without_deploying(card, qtbot):
    """长按到期只交付当前卡片，由父层创建拖动而非触发部署。"""
    card.set_reorder_enabled(True)
    clicks, requests, toggles = [], [], []
    card.clicked.connect(clicks.append)
    card.reorder_requested.connect(requests.append)
    card.toggle_requested.connect(toggles.append)
    with qtbot.waitSignal(card.reorder_requested, timeout=1000) as signal:
        qtbot.mousePress(card, Qt.MouseButton.LeftButton, pos=QPoint(36, 36))
        assert clicks == ["mod-one"]
        assert card._press_timer.interval() == 220
        assert card._press_timer.isSingleShot()
    assert signal.args == [card]
    assert requests == [card]
    assert toggles == []
    assert not card._press_timer.isActive()
    qtbot.wait(240)
    assert requests == [card]
    qtbot.mouseRelease(card, Qt.MouseButton.LeftButton)


def test_left_move_requires_press_and_system_drag_distance(card, qtbot):
    """只有有效左键按下后的阈值移动触发一次排序请求。"""
    card.set_reorder_enabled(True)
    requests = []
    card.reorder_requested.connect(requests.append)
    position = QPoint(36, 36)
    distance = QApplication.startDragDistance()
    _move(card, position + QPoint(distance, 0))
    assert requests == []
    qtbot.mousePress(card, Qt.MouseButton.LeftButton, pos=position)
    assert card._press_pos == position
    _move(card, position + QPoint(distance - 1, 0))
    _move(card, position + QPoint(distance, 0), Qt.MouseButton.RightButton)
    _move(card, position + QPoint(distance, 0), Qt.MouseButton.NoButton)
    assert requests == []
    assert card._press_timer.isActive()
    _move(card, position + QPoint(distance, 0))
    _move(card, position + QPoint(distance + 1, 0))
    assert requests == [card]
    assert not card._press_timer.isActive()
    qtbot.mouseRelease(card, Qt.MouseButton.LeftButton)


@pytest.mark.parametrize("binding", ["unbound", "empty_id", "hidden"])
def test_reorder_rejects_empty_or_hidden_binding(qtbot, binding):
    """排序请求校验当前绑定和可见性，不接受空卡片或隐藏池卡片。"""
    mod = None if binding == "unbound" else Mod(mod_id="" if binding == "empty_id" else "mod-one")
    widget = ModCard(mod)
    qtbot.addWidget(widget)
    widget.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
    if binding != "hidden":
        widget.show()
    widget.set_reorder_enabled(True)
    requests = []
    widget.reorder_requested.connect(requests.append)
    qtbot.mousePress(widget, Qt.MouseButton.LeftButton, pos=QPoint(36, 36))
    widget._request_reorder()
    assert requests == []
    assert not widget._press_timer.isActive()


@pytest.mark.parametrize(
    "operation",
    ["disable", "release", "double_click", "hide", "set_mod", "set_mod_deferred", "same_mod", "same_mod_deferred"],
)
def test_pending_long_press_is_cancelled_before_pool_reuse(card, qtbot, operation):
    """释放、双击、禁用、隐藏和两种重新绑定都取消尚未触发的长按。"""
    card.set_reorder_enabled(True)
    requests = []
    card.reorder_requested.connect(requests.append)
    qtbot.mousePress(card, Qt.MouseButton.LeftButton, pos=QPoint(36, 36))
    assert card._press_timer.isActive()
    if operation == "disable":
        card.set_reorder_enabled(False)
    elif operation == "release":
        qtbot.mouseRelease(card, Qt.MouseButton.LeftButton)
    elif operation == "double_click":
        qtbot.mouseDClick(card, Qt.MouseButton.LeftButton)
    elif operation == "hide":
        card.hide()
        card.show()
    elif operation.startswith("same_mod"):
        method = "set_mod_deferred" if operation.endswith("deferred") else "set_mod"
        getattr(card, method)(card._mod)
    else:
        getattr(card, operation)(Mod(mod_id="mod-two", name="新绑定"))
    assert not card._press_timer.isActive()
    qtbot.wait(240)
    assert requests == []


@pytest.mark.parametrize("enabled", [False, True])
def test_ordinary_click_and_double_click_keep_selection_and_toggle_signals(card, qtbot, enabled):
    """排序开关不改变单击选择和双击启停信号，也不会启动迟到的拖动。"""
    card.set_reorder_enabled(enabled)
    clicks, doubles, toggles, requests = [], [], [], []
    card.clicked.connect(clicks.append)
    card.double_clicked.connect(doubles.append)
    card.toggle_requested.connect(toggles.append)
    card.reorder_requested.connect(requests.append)
    qtbot.mouseClick(card, Qt.MouseButton.LeftButton, pos=QPoint(36, 36))
    assert clicks == ["mod-one"]
    assert doubles == toggles == []
    assert not card._press_timer.isActive()
    qtbot.mouseDClick(card, Qt.MouseButton.LeftButton, pos=QPoint(36, 36))
    assert doubles == toggles == ["mod-one"]
    assert not card._press_timer.isActive()
    qtbot.wait(240)
    assert requests == []


def test_tag_click_and_selected_preview_survive_reorder_toggle(card, qtbot, tmp_path):
    """抓手不会拦截标签点击，启用排序也不重置用户选定的预览。"""
    pixmap = QPixmap(32, 32)
    pixmap.fill(Qt.GlobalColor.red)
    preview_path = str(tmp_path / "selected.png")
    assert pixmap.save(preview_path)
    assert card.show_preview_by_path(preview_path, pixmap)
    cached_pixmap = card._cached_pixmap
    card.set_reorder_enabled(True)
    tags, requests, clicks = [], [], []
    card.tag_clicked.connect(tags.append)
    card.reorder_requested.connect(requests.append)
    card.clicked.connect(clicks.append)
    qtbot.mouseClick(card._tag_row._chips[0], Qt.MouseButton.LeftButton)
    assert tags == ["local:shared"]
    assert requests == clicks == []
    assert not card._press_timer.isActive()
    card.set_reorder_enabled(False)
    assert card.effective_preview_path() == preview_path
    assert card._cached_pixmap is cached_pixmap


def test_context_menu_preserves_plugin_action_without_reorder(card, monkeypatch):
    """右键插件菜单仍执行绑定 Mod 的动作，不触发排序或部署。"""
    card.set_reorder_enabled(True)
    actions, requests, toggles = [], [], []
    monkeypatch.setattr(
        plugin_menu,
        "_provider",
        lambda location, mod_id: [("插件动作", lambda: actions.append((location, mod_id)))],
    )

    dismiss = QTimer(card)
    dismiss.setInterval(10)

    def execute_menu():
        """菜单实际显示后执行插件动作并关闭原生弹出循环。"""
        menu = QApplication.activePopupWidget()
        if isinstance(menu, QMenu):
            dismiss.stop()
            menu.actions()[0].trigger()
            menu.close()

    dismiss.timeout.connect(execute_menu)
    card.reorder_requested.connect(requests.append)
    card.toggle_requested.connect(toggles.append)
    position = QPoint(36, 36)
    event = QContextMenuEvent(QContextMenuEvent.Reason.Mouse, position, card.mapToGlobal(position))
    dismiss.start()
    try:
        QApplication.sendEvent(card, event)
    finally:
        dismiss.stop()
    assert event.isAccepted()
    assert actions == [("mod-context", "mod-one")]
    assert requests == toggles == []
    assert not card._press_timer.isActive()
