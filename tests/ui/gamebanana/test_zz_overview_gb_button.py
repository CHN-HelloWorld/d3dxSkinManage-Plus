from unittest.mock import MagicMock

import pytest


@pytest.fixture
def overview_page(qtbot, tmp_path, monkeypatch):
    # 隔离 data/config 目录与单例
    data_dir = tmp_path / "data"
    config_dir = tmp_path / "config"
    data_dir.mkdir(parents=True, exist_ok=True)
    config_dir.mkdir(parents=True, exist_ok=True)

    from src.utils import paths as paths_mod

    monkeypatch.setattr(paths_mod, "get_data_dir", lambda: data_dir)
    monkeypatch.setattr(paths_mod, "get_config_dir", lambda: config_dir)
    monkeypatch.setattr(paths_mod, "get_app_dir", lambda: tmp_path)

    import sys

    for mod_name, mod in list(sys.modules.items()):
        if mod is None or not mod_name.startswith("src."):
            continue
        for fn_name, replacement in (
            ("get_data_dir", lambda: data_dir),
            ("get_config_dir", lambda: config_dir),
            ("get_app_dir", lambda: tmp_path),
        ):
            if hasattr(mod, fn_name):
                monkeypatch.setattr(mod, fn_name, replacement, raising=False)

    # 清空单例
    from src.core import config as config_mod

    config_mod.Config._instance = None
    try:
        from src.core import mod_manager as mm_mod

        if hasattr(mm_mod.ModManager, "_instance"):
            mm_mod.ModManager._instance = None
    except Exception:
        pass
    try:
        from src.core import game_service as gs_mod

        if hasattr(gs_mod.GameService, "_instance"):
            gs_mod.GameService._instance = None
    except Exception:
        pass

    from src.ui.pages.overview import OverviewPage

    page = OverviewPage()
    qtbot.addWidget(page)
    yield page

    # 显式销毁所有子 widget，防止累积导致 Qt shutdown 缓冲溢出
    try:
        from PySide6.QtWidgets import QApplication

        # 显式停止 OverviewPage 中无 parent 的 QTimer（_resize_timer/_scale_timer），
        # 否则它们在 Python 端被回收时 Qt 端 timerId 仍挂在 event loop 上，
        # 累积后触发 STATUS_STACK_BUFFER_OVERRUN
        for attr in ("_search_timer", "_resize_timer", "_scale_timer", "_vs_scroll_timer", "_preview_load_timer"):
            t = getattr(page, attr, None)
            if t is not None:
                try:
                    t.stop()
                    t.deleteLater()
                except Exception:
                    pass
        page.setParent(None)
        page.close()
        page.deleteLater()
        QApplication.processEvents()
        QApplication.processEvents()
        import gc

        gc.collect()
        QApplication.processEvents()
    except Exception:
        pass

    # 测试后清单例
    config_mod.Config._instance = None
    try:
        from src.core import mod_manager as mm_mod

        if hasattr(mm_mod.ModManager, "_instance"):
            mm_mod.ModManager._instance = None
    except Exception:
        pass
    try:
        from src.core import game_service as gs_mod

        if hasattr(gs_mod.GameService, "_instance"):
            gs_mod.GameService._instance = None
    except Exception:
        pass


def test_gb_search_button_enabled_for_category(overview_page):
    overview_page._selected_category = {"id": "cat1", "name": "Sigma", "name_en": ""}
    overview_page._show_category_panel(overview_page._selected_category)
    # 按钮在类别模式下应当 enabled（visible 受 widget tree 状态影响，但 enable 状态可靠）
    assert overview_page._panel_gb_search_btn.isEnabled()


def test_gb_search_calls_main_window(overview_page):
    overview_page._selected_category = {"id": "cat1", "name": "示例角色丑", "name_en": "Sigma"}
    overview_page._show_category_panel(overview_page._selected_category)
    # 用替身提供父窗口的页面切换入口。
    fake_main = MagicMock()
    overview_page.window = lambda: fake_main
    overview_page._on_panel_gb_search()
    fake_main.switch_to_mod_store_with_query.assert_called_once_with("Sigma")


def test_gb_button_disabled_when_config_disabled(overview_page):
    # 关闭 Config 开关
    from src.core.config import Config

    Config().set("gamebanana.character_to_search_button", False)
    overview_page._selected_category = {"id": "cat1", "name": "X", "name_en": ""}
    overview_page._show_category_panel(overview_page._selected_category)
    assert not overview_page._panel_gb_search_btn.isEnabled()
    # 还原默认值，避免影响其他测试
    Config().set("gamebanana.character_to_search_button", True)


def test_gb_button_hidden_in_mod_mode(overview_page):
    from src.models.mod import AgeRating, Mod, ModInfo, ModStatus

    mod = Mod(
        game_id="game",
        mod_id="m1",
        name="Test",
        folder_name="test",
        path="",
        status=ModStatus.DISABLED,
        info=ModInfo(age_rating=AgeRating.G),
    )
    overview_page._selected_mod = mod
    overview_page._show_mod_panel(mod)
    assert not overview_page._panel_gb_search_btn.isEnabled()


def test_gb_search_noop_without_category(overview_page):
    overview_page._selected_category = None
    fake_main = MagicMock()
    overview_page.window = lambda: fake_main
    overview_page._on_panel_gb_search()
    fake_main.switch_to_mod_store_with_query.assert_not_called()
