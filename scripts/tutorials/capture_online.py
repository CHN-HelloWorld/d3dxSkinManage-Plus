"""用中性占位记录填充真实在线页面，仅用于教程截图。"""

from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch


class PlaceholderImages:
    """从演示目录读取占位图片，避免连接任何在线服务。"""

    def __init__(self, workspace: Path):
        """固定允许读取的演示根目录。"""
        self.workspace = workspace.resolve()

    def load(self, url, target, *, size=None, **kwargs):
        """通过实际图片控件显示本地占位图。"""
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QPixmap

        source = Path(url).resolve()
        if not source.is_relative_to(self.workspace):
            raise ValueError("占位图片越出演示工作区。")
        pixmap = QPixmap(str(source))
        if size is not None and not size.isEmpty():
            pixmap = pixmap.scaled(size, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        target.setPixmap(pixmap)

    def try_cached(self, url, target, **kwargs):
        """占位图片已在本地，直接交付。"""
        self.load(url, target, **kwargs)
        return True

    def cancel_for(self, target):
        """同步加载没有待取消的后台请求。"""


def capture_online(win, workspace, shot, want):
    """以真实数据模型和真实控件展示占位在线列表及详情。"""
    from PySide6.QtWidgets import QApplication

    from src.services.gamebanana.models import GbAuthor, GbCategory, GbModel, GbModFile, GbModProfile, GbModRecord

    store = win._gb_store_page
    store._pending_initial_game_id = None
    store._request_gen += 1
    win._select_nav_page(type(win).PAGE_STORE)
    preview_paths = sorted((workspace / "demo/previews").glob("*.png"))
    records = [
        GbModRecord(
            mod_id=90000 + i,
            model=GbModel.MOD,
            name=f"示例在线 Mod {chr(65 + i)}",
            submitter=GbAuthor(1, "示例作者", "", "https://example.invalid/author"),
            thumbnail_url=str(path),
            page_url=f"https://example.invalid/sample-{i}",
            view_count=100,
            like_count=10,
            download_count=20,
            comment_count=0,
            is_nsfw=False,
            date_updated_ts=1793318400,
        )
        for i, path in enumerate(preview_paths)
    ]
    loader = PlaceholderImages(workspace)
    with ExitStack() as stack:
        stack.enter_context(patch.object(store, "_loader", loader))
        stack.enter_context(patch.object(store.detail_view, "_image_loader", loader))
        store.error_bar.hide()
        store.stack.setCurrentWidget(store.browse_view)
        store.breadcrumb.set_path(
            [
                GbCategory(0, "示例游戏（占位）", None, 6, None),
                GbCategory(-1, "Mods", None, 6, None),
                GbCategory(1, "示例分类", None, 6, None),
            ]
        )
        store._all_records = records
        store._has_more = False
        store._rerender_from_cache()
        if want("24"):
            shot("24-在线Mod", win, settle=900)
        if want("29"):
            profile = GbModProfile(
                **vars(records[0]),
                description_html="<p>此内容是教程占位数据，用于说明下载文件和查看详情的入口。</p>",
                preview_images=[records[0].thumbnail_url],
                files=[
                    GbModFile(
                        90000 + i,
                        f"sample-mod-{i + 1}.zip",
                        "示例文件，仅用于展示界面",
                        f"https://example.invalid/sample-{i + 1}.zip",
                        1048576 * (i + 1),
                        1793318400,
                        "",
                        "",
                        version="1.0",
                    )
                    for i in range(2)
                ],
                category_name="示例分类",
            )
            store._show_detail_floating()
            QApplication.processEvents()
            store.detail_view.show_loading_for(records[0])
            # 先让可见面板的连接提示完成布局，再交付占位响应，避免延迟提示盖住已加载内容。
            QApplication.processEvents()
            store.detail_view.load_profile(profile)
            QApplication.processEvents()
            if store.detail_view._loading_overlay.isVisible():
                raise RuntimeError("在线详情仍在加载，不生成未完成的操作截图。")
            shot("29-在线详情", win, settle=900)
            store._hide_detail_floating()
