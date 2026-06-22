from __future__ import annotations

import pytest


@pytest.fixture
def native_qt_teardown(qapp, temp_data_dir):
    """恢复隔离数据目录前销毁已关闭的原生控件。"""
    from PySide6.QtCore import QEvent

    yield
    # processEvents 不会在主事件循环外清空 DeferredDelete，需显式处理，
    # 防止循环垃圾回收先释放仍被 Qt 使用的 Python 回调。
    qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qapp.processEvents()


@pytest.fixture(autouse=True, scope="session")
def _disable_ui_animations():
    """关闭装饰动画，使普通控件测试直接验证交互终态。

    pytest-qt 在用例结束时通过 deleteLater 释放控件，待处理动画可能跨越测试边界。
    立即应用动画终值可避免回调访问待销毁对象；动画行为由专门用例单独启用和验证。
    """
    try:
        from src.ui import anim

        anim.set_enabled(False)
    except Exception:
        pass
    yield


@pytest.fixture
def temp_data_dir(tmp_path, monkeypatch):
    """重定向 data/config 目录到 tmp_path，并清空相关单例。

    由于多个模块以 `from src.utils.paths import get_*` 形式导入路径函数，
    monkeypatch 需要 (a) patch 源模块 (b) patch 所有已导入的消费者模块的
    本地绑定，否则消费者会获取原始函数。
    """
    data_dir = tmp_path / "data"
    config_dir = tmp_path / "config"
    data_dir.mkdir(parents=True, exist_ok=True)
    config_dir.mkdir(parents=True, exist_ok=True)

    # 1. patch 源模块（防止后续才被导入的模块获取原函数）
    from src.utils import paths as paths_mod

    monkeypatch.setattr(paths_mod, "get_data_dir", lambda: data_dir)
    monkeypatch.setattr(paths_mod, "get_config_dir", lambda: config_dir)
    monkeypatch.setattr(paths_mod, "get_app_dir", lambda: tmp_path)

    # 2. patch 已经做了 `from src.utils.paths import get_*` 的消费者
    import sys

    for mod_name, mod in list(sys.modules.items()):
        if mod is None:
            continue
        if not mod_name.startswith("src."):
            continue
        for fn_name, replacement in (
            ("get_data_dir", lambda: data_dir),
            ("get_config_dir", lambda: config_dir),
            ("get_app_dir", lambda: tmp_path),
        ):
            if hasattr(mod, fn_name):
                monkeypatch.setattr(mod, fn_name, replacement, raising=False)

    # 3. 清空相关单例
    from src.core import config as config_mod

    config_mod.Config._instance = None

    # 游戏配置由独立单例管理，必须重建实例以绑定当前用例的隔离目录。
    from src.core import game_config_store as game_store_mod

    game_store_mod.GameConfigStore._instance = None

    try:
        from src.core import preset_manager as pm_mod

        if hasattr(pm_mod, "PresetManager") and hasattr(pm_mod.PresetManager, "_instance"):
            pm_mod.PresetManager._instance = None
    except Exception:
        pass

    try:
        from src.core import mod_manager as mm_mod

        if hasattr(mm_mod, "ModManager") and hasattr(mm_mod.ModManager, "_instance"):
            mm_mod.ModManager._instance = None
    except Exception:
        pass

    yield tmp_path

    # 测试后清空单例，防止跨测试污染
    config_mod.Config._instance = None
    game_store_mod.GameConfigStore._instance = None
    try:
        from src.core import mod_manager as mm_mod

        if hasattr(mm_mod, "ModManager") and hasattr(mm_mod.ModManager, "_instance"):
            mm_mod.ModManager._instance = None
    except Exception:
        pass
