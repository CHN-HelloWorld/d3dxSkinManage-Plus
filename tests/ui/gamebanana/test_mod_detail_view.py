from PySide6.QtWidgets import QLabel

from src.services.gamebanana.models import (
    GbAuthor,
    GbModel,
    GbModFile,
    GbModProfile,
    GbModRecord,
)
from src.ui.widgets.gb_mod_detail_view import GbModDetailView


def _widget_count(layout):
    return sum(layout.itemAt(index).widget() is not None for index in range(layout.count()))


def _rec(rid=1, name="First Mod"):
    return GbModRecord(
        mod_id=rid,
        model=GbModel.MOD,
        name=name,
        submitter=GbAuthor(id=1, name="Alice", avatar_url="", profile_url=""),
        thumbnail_url="",
        page_url="",
        view_count=0,
        like_count=0,
        download_count=0,
        comment_count=0,
        is_nsfw=False,
        date_updated_ts=0,
    )


def _profile(mid=1, name="First Mod", files=1, previews=2):
    return GbModProfile(
        mod_id=mid,
        model=GbModel.MOD,
        name=name,
        submitter=GbAuthor(id=1, name="Alice", avatar_url="", profile_url=""),
        thumbnail_url="",
        page_url="",
        view_count=0,
        like_count=0,
        download_count=0,
        comment_count=0,
        is_nsfw=False,
        date_updated_ts=0,
        description_html="<p>desc-1</p>",
        preview_images=[f"https://x/p{i}.jpg" for i in range(previews)],
        files=[
            GbModFile(
                file_id=10 + i,
                file_name=f"f{i}.zip",
                description="",
                download_url="",
                size_bytes=1024 * 1024 * (i + 1),
                date_added_ts=0,
                md5_checksum=f"md5{i}",
                av_result="",
            )
            for i in range(files)
        ],
    )


def test_tab_labels_are_chinese(qtbot, fake_image_loader):
    view = GbModDetailView(image_loader=fake_image_loader)
    qtbot.addWidget(view)
    labels = [view.tabs.tabText(i) for i in range(view.tabs.count())]
    assert labels == ["概览", "描述", "更新"]


def test_show_loading_for_clears_previous_profile(qtbot, fake_image_loader):
    view = GbModDetailView(image_loader=fake_image_loader)
    qtbot.addWidget(view)
    view.load_profile(_profile(mid=1, name="First Mod", files=2, previews=3))
    assert view.title_label.text() == "First Mod"
    assert _widget_count(view.previews_layout) == 3
    assert _widget_count(view.files_layout) == 2
    # 模拟点击第二个 Mod
    view.show_loading_for(_rec(rid=2, name="Second Mod"))
    # 立即检查：标题应已更新，但旧的预览/文件应已清空
    assert view.title_label.text() == "Second Mod"
    assert _widget_count(view.previews_layout) == 0
    assert _widget_count(view.files_layout) == 0


def test_previews_are_vertical_no_horizontal_scrollbar(qtbot, fake_image_loader):
    from PySide6.QtCore import Qt

    view = GbModDetailView(image_loader=fake_image_loader)
    qtbot.addWidget(view)
    view.load_profile(_profile(previews=4))
    assert view.preview_scroll_area.horizontalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    # 容器应该是垂直 layout
    from PySide6.QtWidgets import QVBoxLayout

    assert isinstance(view.previews_layout, QVBoxLayout)


def test_files_section_has_downloadable_separator_label(qtbot, fake_image_loader):
    view = GbModDetailView(image_loader=fake_image_loader)
    qtbot.addWidget(view)
    view.load_profile(_profile(files=1))
    assert view.separator_label.text() == "可下载文件"


def test_file_row_uses_humanized_size(qtbot, fake_image_loader):
    view = GbModDetailView(image_loader=fake_image_loader)
    qtbot.addWidget(view)
    p = _profile(files=1)  # 第一个文件 1 MB
    view.load_profile(p)
    row = view.files_layout.itemAt(0).widget()
    # 文件名称和大小由两个标签分别显示。
    text = " ".join(label.text() for label in row.findChildren(QLabel))
    assert "1.0 MB" in text
    assert "KB" not in text  # 文件大小单位随数值范围变化。


def test_back_button_emits_signal(qtbot, fake_image_loader):
    view = GbModDetailView(image_loader=fake_image_loader)
    qtbot.addWidget(view)
    with qtbot.waitSignal(view.back_requested, timeout=1000):
        view.back_btn.click()
