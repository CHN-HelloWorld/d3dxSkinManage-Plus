from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QLabel

from src.services.gamebanana.models import GbModFile
from src.services.gamebanana.update_checker import UpdateInfo
from src.ui.dialogs.mod_updates_dialog import ModUpdatesDialog


def _info(mid, name="示例角色丁旗袍", cur="v1.0", new="v1.3"):
    f = GbModFile(
        file_id=11,
        file_name="s.zip",
        description="",
        download_url="http://x",
        size_bytes=1,
        date_added_ts=2000,
        md5_checksum="bbb",
        av_result="clean",
        version=new,
    )
    return UpdateInfo(
        local_mod_id=mid,
        gb_mod_id=123,
        current_version=cur,
        current_file_ts=1000,
        latest_file=f,
        latest_version=new,
        candidate_files=[f],
        changelog=[],
        page_url="http://p",
        local_name=name,
    )


def keep_dialog(qtbot, widget):
    # qtbot 只持有弱引用，需保持顶级父窗口存活至清理结束。
    if not hasattr(qtbot, "_update_dialog_roots"):
        qtbot._update_dialog_roots = []
    qtbot._update_dialog_roots.append(widget)
    qtbot.addWidget(widget)


def test_lists_rows(qtbot):
    dlg = ModUpdatesDialog([_info("m1"), _info("m2", name="示例角色午西装")], failures={})
    keep_dialog(qtbot, dlg)
    assert dlg.row_count() == 2


def test_failure_footer_shown(qtbot):
    dlg = ModUpdatesDialog([], failures={"m9": "网络错误"})
    keep_dialog(qtbot, dlg)
    assert "1 项" in dlg._check_panel.title.text()
    assert dlg._check_panel.isVisibleTo(dlg)


def test_row_shows_thumbnail(qtbot, tmp_path):
    img = tmp_path / "preview.png"
    pm = QPixmap(40, 40)
    pm.fill()  # 默认白色
    assert pm.save(str(img))
    info = _info("m1")
    info.local_preview = str(img)
    dlg = ModUpdatesDialog([info], failures={})
    keep_dialog(qtbot, dlg)
    thumbs = dlg.findChildren(QLabel, "updateThumb")
    assert thumbs, "缩略图 QLabel(objectName=updateThumb) 未渲染"
    pmap = thumbs[0].pixmap()
    assert pmap is not None and not pmap.isNull()


def test_row_thumbnail_placeholder_when_missing(qtbot):
    info = _info("m1")
    info.local_preview = ""
    dlg = ModUpdatesDialog([info], failures={})
    keep_dialog(qtbot, dlg)
    thumbs = dlg.findChildren(QLabel, "updateThumb")
    assert thumbs and thumbs[0].text() == "暂无预览"
