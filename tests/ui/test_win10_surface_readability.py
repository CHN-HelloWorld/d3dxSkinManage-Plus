"""验证回退模糊的弹层底色能遮挡下层细节，并保留圆角透明边缘。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QDialog, QMenu

from src.ui import acrylic, theme, win_effects


@pytest.fixture
def fallback_appearance(qapp, monkeypatch):
    """隔离主题和平台版本，用实际 Qt 绘制检查 Windows 10 的回退表面。"""
    previous = qapp.styleSheet(), theme.current_theme(), theme.is_background_active()
    monkeypatch.setattr(
        win_effects,
        "sys",
        SimpleNamespace(platform="win32", getwindowsversion=lambda: SimpleNamespace(build=19045)),
    )
    popup_filter = acrylic.AcrylicPopupFilter()
    qapp.installEventFilter(popup_filter)

    def apply(mode: str) -> None:
        """切换测试主题并使用宿主的实际样式表。"""
        theme.set_theme(mode)
        theme.set_background_active(True)
        qapp.setStyleSheet(theme.appearance_qss(mode))

    yield apply
    qapp.removeEventFilter(popup_filter)
    theme.set_theme(previous[1])
    theme.set_background_active(previous[2])
    qapp.setStyleSheet(previous[0])


def _surface(kind: str, qtbot, tmp_path):
    """创建真实业务弹层，返回其拥有者、绘制表面和需要关闭的下载管理器。"""
    manager = None
    if kind == "dialog":
        owner = popup = QDialog()
        acrylic.apply_dialog_acrylic(popup, animate=False)
    elif kind == "preset":
        from src.ui.widgets.preset_selector import PresetPopup

        owner = popup = PresetPopup()
    elif kind == "category":
        from src.services.gamebanana.models import GbCategory
        from src.ui.widgets.gb_breadcrumb_bar import GbBreadcrumbBar

        owner = GbBreadcrumbBar()
        owner.set_path(
            [
                GbCategory(id=0, name="示例游戏", parent_id=None, item_count=0, icon_url=None),
                GbCategory(id=1, name="示例分类", parent_id=0, item_count=0, icon_url=None),
            ]
        )
        popup = owner._segments[1].menu()
        owner._populate_menu(1)
    elif kind == "download":
        from src.services.gamebanana.downloader import GbDownloadManager
        from src.ui.widgets.gb_download_flyout import GbDownloadFlyout

        manager = GbDownloadManager(base_dir=tmp_path)
        owner = popup = GbDownloadFlyout(manager)
    elif kind == "tag":
        from src.ui.widgets.tag_display import TagPopup

        owner = popup = TagPopup()
    else:
        owner = popup = QMenu()
        popup.addAction("示例操作")
    qtbot.addWidget(owner)
    popup.resize(440, 360)
    return owner, popup, manager


@pytest.mark.parametrize("mode", ["light", "dark"])
@pytest.mark.parametrize("kind", ["dialog", "preset", "category", "download", "tag", "menu"])
def test_win10_frosted_surfaces_limit_background_detail(
    qtbot, tmp_path, temp_data_dir, fallback_appearance, mode, kind
):
    """下层黑白细节的最大透出差不超过 20 色阶，面板仍保留轻微透明度。"""
    fallback_appearance(mode)
    owner, popup, manager = _surface(kind, qtbot, tmp_path)
    try:
        popup.show()
        image = popup.grab().toImage()
        ratio = image.devicePixelRatio()
        color = image.pixelColor(round(popup.width() / 2 * ratio), round(4 * ratio))
        assert 0 < 255 - color.alpha() <= 20, f"{kind} 的底色不足以隔离下层内容：{color.getRgb()}"
        assert image.pixelColor(0, 0).alpha() == 0
    finally:
        popup.hide()
        owner.close()
        if manager is not None:
            manager.shutdown()


@pytest.mark.parametrize("kind, alpha_range", [("dialog", (0.48, 0.52)), ("preset", (0.70, 0.74))])
def test_win11_surfaces_keep_requested_material(
    qtbot, tmp_path, temp_data_dir, fallback_appearance, monkeypatch, kind, alpha_range
):
    """Windows 11 继续使用调用方指定的叠色，不套用回退平台的较厚底色。"""
    monkeypatch.setattr(win_effects.sys, "getwindowsversion", lambda: SimpleNamespace(build=22631))
    fallback_appearance("light")
    owner, popup, _ = _surface(kind, qtbot, tmp_path)
    popup.show()
    image = popup.grab().toImage()
    ratio = image.devicePixelRatio()
    color = image.pixelColor(round(popup.width() / 2 * ratio), round(4 * ratio))
    assert alpha_range[0] <= color.alphaF() <= alpha_range[1]
    owner.close()
