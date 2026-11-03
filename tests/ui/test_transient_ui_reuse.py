"""高频临时界面复用有限实例，同时刷新数据并释放关闭后的大块内容。"""

from __future__ import annotations

from types import SimpleNamespace
from weakref import ref

import pytest
import shiboken6
from PySide6.QtCore import QEvent, QPoint, QTimer
from PySide6.QtWidgets import QLabel, QMenu, QPushButton, QWidget

from src.ui import acrylic, theme
from src.ui.dialogs import leftover_intake, log_viewer
from src.ui.main_window import MainWindow
from src.ui.pages.game import GamePage
from tests.helpers import reset_game_service
from tests.qt_dialogs import interact


def _flush(qapp):
    """完成延迟销毁，避免把尚未投递的 Qt 删除事件误判为存活对象。"""
    qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qapp.processEvents()


@pytest.fixture
def host(qapp, temp_data_dir):
    """提供没有后台任务的长寿命父窗口，业务入口直接使用主窗口的真实方法。"""
    widget = QWidget()
    yield widget
    if shiboken6.isValid(widget):
        widget.close()
        widget.deleteLater()
    _flush(qapp)


@pytest.fixture
def game_page(qapp, temp_data_dir):
    """创建真实游戏页并允许测试主动销毁所有者，避免测试框架重复关闭失效对象。"""
    reset_game_service()
    page = GamePage()
    yield page
    if shiboken6.isValid(page):
        page.close()
        page.deleteLater()
    _flush(qapp)
    reset_game_service()


def test_logs_reuse_window_refresh_new_text_and_release_hidden_content(qapp, host, tmp_path, monkeypatch):
    logs = tmp_path / "logs" / "2026-11-03"
    logs.mkdir(parents=True)
    logfile = logs / "1.log"
    monkeypatch.setattr(log_viewer, "get_log_dir", lambda: logs.parent)
    monkeypatch.setattr(log_viewer, "get_log_file", lambda: logfile)
    dialogs = []
    for index in range(4):
        logfile.write_text(f"[2026-11-03] [INFO] entry-{index}\n", encoding="utf-8")

        def inspect(dialog):
            """每次重开读取刚写入的内容，确认缓存不会显示上次会话正文。"""
            dialogs.append(dialog)
            assert dialog._view.toPlainText() == f"[2026-11-03] [INFO] entry-{index}"
            dialog.reject()

        with interact(log_viewer.LogViewerDialog, inspect):
            MainWindow._on_view_logs(host)
        _flush(qapp)
    assert len({id(dialog) for dialog in dialogs}) == 1
    assert len(host.findChildren(log_viewer.LogViewerDialog)) == 1
    assert dialogs[-1]._raw_lines == []
    assert dialogs[-1]._view.toPlainText() == ""
    host.deleteLater()
    _flush(qapp)
    assert not shiboken6.isValid(dialogs[-1])


def test_log_window_rethemes_without_rebuilding_controls(qapp, host, monkeypatch, tmp_path):
    monkeypatch.setattr(log_viewer, "get_log_dir", lambda: tmp_path)
    previous = theme.current_theme()
    dialogs = []
    views = []
    try:
        for selected in ("dark", "light"):
            theme.set_theme(selected)

            def inspect(dialog):
                """同一文本控件应在重开时应用当前主题色。"""
                dialogs.append(dialog)
                views.append(dialog._view)
                assert theme.color("text.primary") in dialog._view.styleSheet()
                assert theme.color("bg.subtle") in dialog._view.styleSheet()
                dialog.reject()

            with interact(log_viewer.LogViewerDialog, inspect):
                MainWindow._on_view_logs(host)
            _flush(qapp)
        assert dialogs[0] is dialogs[1]
        assert views[0] is views[1]
    finally:
        theme.set_theme(previous)


@pytest.mark.parametrize("choose", [False, True])
def test_plugin_command_menu_reuses_window_and_discards_old_actions(qapp, host, choose):
    host._plugin_cmd_btn = QPushButton(host)
    menus = []
    actions = []
    invoked = []
    for index in range(4):
        host._build_mod_context_actions = lambda *_args: [(f"command-{index}", lambda i=index: invoked.append(i))]

        def inspect(menu):
            """显示本次贡献的命令，记录动作供关闭后验证不再被菜单保留。"""
            menus.append(menu)
            assert [action.text() for action in menu.actions()] == [f"command-{index}"]
            actions.extend(menu.actions())
            return menu.actions()[0] if choose else None

        with interact(QMenu, inspect):
            MainWindow._show_plugin_command_menu(host)
        _flush(qapp)
    assert invoked == ([0, 1, 2, 3] if choose else [])
    assert len({id(menu) for menu in menus}) == 1
    assert menus[-1].actions() == []
    assert all(not shiboken6.isValid(action) for action in actions)
    host.deleteLater()
    _flush(qapp)
    assert not shiboken6.isValid(menus[-1])


def test_plugin_command_callback_can_reopen_the_same_menu(qapp, host):
    host._plugin_cmd_btn = QPushButton(host)
    menus = []
    invoked = []

    def choose(menu):
        """选择本轮唯一命令，并保留窗口引用核对嵌套回调重用的对象。"""
        menus.append(menu)
        return menu.actions()[0]

    def reopen():
        """当前命令回调可以同步重开菜单，旧动作须在新一轮填充前完成判定。"""
        invoked.append("outer")
        host._build_mod_context_actions = lambda *_args: [("inner", lambda: invoked.append("inner"))]
        with interact(QMenu, choose):
            MainWindow._show_plugin_command_menu(host)

    host._build_mod_context_actions = lambda *_args: [("outer", reopen)]
    with interact(QMenu, choose):
        MainWindow._show_plugin_command_menu(host)
    _flush(qapp)
    assert invoked == ["outer", "inner"]
    assert len(menus) == 2 and menus[0] is menus[1]
    assert menus[0].actions() == []


def test_game_menus_share_one_window_and_dispatch_only_current_actions(qapp, game_page, monkeypatch):
    page = game_page
    page._svc = SimpleNamespace(get_game=lambda _: object(), has_active_game=lambda: True, current_game_id="active")
    invoked = []
    monkeypatch.setattr(page, "_on_delete", lambda game_id: invoked.append(("delete", game_id)))
    monkeypatch.setattr(page, "_on_add", lambda: invoked.append(("add", "")))
    menus = []
    actions = []
    for selected in ("first", "", "second", ""):

        def choose(menu):
            """交替选择游戏删除与空白添加，防止复用菜单派发上次的游戏标识。"""
            menus.append(menu)
            actions.extend(menu.actions())
            return menu.actions()[-1] if selected else menu.actions()[0]

        with interact(QMenu, choose):
            if selected:
                page._show_game_context_menu(selected, QPoint())
            else:
                page._show_blank_context_menu(QPoint())
        _flush(qapp)
    assert invoked == [("delete", "first"), ("add", ""), ("delete", "second"), ("add", "")]
    assert len({id(menu) for menu in menus}) == 1
    assert menus[0].actions() == []
    assert all(not shiboken6.isValid(action) for action in actions)
    page.deleteLater()
    _flush(qapp)
    assert not shiboken6.isValid(menus[0])


def test_cached_game_menu_follows_existing_theme_refresh(qapp, game_page):
    page = game_page
    popup_filter = acrylic.AcrylicPopupFilter(qapp)
    qapp.installEventFilter(popup_filter)
    previous = theme.current_theme()
    menus = []
    try:
        for selected in ("dark", "light"):
            theme.set_theme(selected)
            acrylic.reset_for_theme_change()

            def inspect(menu):
                """应用既有主题刷新入口后检查复用菜单的内联文字色。"""
                menus.append(menu)
                assert theme.color("text.primary") in menu.styleSheet()

            with interact(QMenu, inspect):
                page._show_blank_context_menu(QPoint())
            _flush(qapp)
        assert menus[0] is menus[1]
    finally:
        qapp.removeEventFilter(popup_filter)
        popup_filter.deleteLater()
        theme.set_theme(previous)
        acrylic.reset_for_theme_change()


@pytest.mark.parametrize("allow_keep", [False, True])
def test_leftovers_reuse_one_batch_window_and_reset_dismissal_state(qapp, host, tmp_path, monkeypatch, allow_keep):
    created = []
    displayed = []
    checked = []
    paths = [tmp_path / "first.zip", tmp_path / "second.zip", tmp_path / "third.zip"]

    class RespondingDialog(leftover_intake.LeftoverIntakeDialog):
        """保留真实布局和模态循环，仅在每次显示后排队执行处置选择。"""

        def __init__(self, *args, **kwargs):
            """用弱引用记录构造数量，不额外延长窗口寿命。"""
            super().__init__(*args, **kwargs)
            created.append(ref(self))

        def showEvent(self, event):
            """窗口显示后记录当前项及复选框状态，再执行当次选择。"""
            super().showEvent(event)
            labels = "\n".join(label.text() for label in self.findChildren(QLabel))
            displayed.append(labels)
            checked.append(self._apply_all_box.isChecked())
            QTimer.singleShot(0, self._respond)

        def _respond(self):
            """第一项勾选后取消，第二项显式回收，第三项取消以检验默认决定重置。"""
            if len(displayed) == 1:
                self._apply_all_box.setChecked(True)
                self.reject()
            elif len(displayed) == 2:
                self._choose(leftover_intake.ACTION_RECYCLE)
            else:
                self.reject()

    monkeypatch.setattr(leftover_intake, "LeftoverIntakeDialog", RespondingDialog)
    decisions = leftover_intake.resolve_leftover_paths(paths, parent=host, allow_keep=allow_keep)
    _flush(qapp)
    default = "keep" if allow_keep else "permanent"
    assert decisions == {str(paths[0]): default, str(paths[1]): "recycle_bin", str(paths[2]): default}
    assert len(displayed) == 3
    assert all(path.name in labels for path, labels in zip(paths, displayed, strict=True))
    assert checked == [False, False, False]
    assert len(created) == 1
    assert not host.findChildren(RespondingDialog)
    assert created[0]() is None or not shiboken6.isValid(created[0]())
