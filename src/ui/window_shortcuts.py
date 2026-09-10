"""窗口命令与界面导航和按钮共用操作入口。"""

from __future__ import annotations

from functools import partial

from PySide6.QtCore import Qt


def _focus(control) -> bool:
    """聚焦可见且可用的输入控件并全选文本，返回是否成功。"""
    if control is None or not control.isVisible() or not control.isEnabled():
        return False
    control.setFocus(Qt.FocusReason.ShortcutFocusReason)
    control.selectAll()
    return True


def focus_current_search(window) -> bool:
    """查找当前页面的搜索输入框并尝试聚焦，未找到可用输入框时返回 False。"""
    page = window._stack.currentWidget()
    if page is getattr(window, "_gb_store_page", None):
        bar = getattr(page, "filter_bar", None)
        return _focus(getattr(bar, "search_box", None))
    for attribute in ("_settings_search", "_search_input", "_search"):
        control = getattr(page, attribute, None)
        if control is not None:
            return _focus(control)
    return False


def _search_settings(window) -> None:
    """切换到设置页，并在切换成功后聚焦设置搜索框。"""
    window._select_nav_page(window.PAGE_SETTINGS)
    if window._stack.currentWidget() is window._settings_page:
        _focus(window._settings_page._settings_search)


def _back(window) -> None:
    """根据当前页面执行分类返回、商店详情退出或商店上级路径导航。"""
    page = window._stack.currentWidget()
    if page is window._overview_page:
        page._navigate_back()
    elif page is window._gb_store_page:
        details = getattr(page, "detail_view", None)
        if details is not None and details.isVisible():
            page._on_back_to_browse()
        elif len(getattr(page, "_path", ())) > 1:
            page._on_path_changed(page._path[:-1])


def _new_category(window) -> None:
    """仅在当前分类总览已选择游戏时触发新增分类操作。"""
    page = window._overview_page
    if window._stack.currentWidget() is page and page._current_game_id:
        page._on_add_character()


def _launch(window, button_name, callback) -> None:
    """仅在对应启动按钮可用时执行启动回调，保持快捷键与按钮权限一致。"""
    button = getattr(window, button_name, None)
    if button is not None and button.isEnabled():
        callback()


def install_window_shortcuts(window, dispatcher) -> None:
    """将搜索、页面导航、分类创建和游戏启动命令绑定到当前窗口动作。"""
    dispatcher.register_window_action("view.search", partial(focus_current_search, window))
    dispatcher.register_window_action("settings.search", partial(_search_settings, window))
    dispatcher.register_window_action("nav.back", partial(_back, window))
    dispatcher.register_window_action("category.new", partial(_new_category, window))
    for command, page in (
        ("nav.game", window.PAGE_GAME),
        ("nav.overview", window.PAGE_OVERVIEW),
        ("nav.gallery", window.PAGE_GALLERY),
        ("nav.store", window.PAGE_STORE),
        ("nav.settings", window.PAGE_SETTINGS),
    ):
        dispatcher.register_window_action(command, partial(window._select_nav_page, page))
    dispatcher.register_window_action(
        "launch.loader", partial(_launch, window, "_launch_loader_btn", window._on_launch_migoto)
    )
    dispatcher.register_window_action(
        "launch.game", partial(_launch, window, "_launch_game_btn", window._on_launch_game)
    )
