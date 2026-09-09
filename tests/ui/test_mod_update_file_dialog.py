from src.services.gamebanana.models import GbModFile
from src.ui.dialogs.mod_update_file_dialog import ModUpdateFileDialog


def _f(fid, name, ts=2000):
    return GbModFile(
        file_id=fid,
        file_name=name,
        description="",
        download_url="http://x",
        size_bytes=1024,
        date_added_ts=ts,
        md5_checksum=f"md{fid}",
        av_result="clean",
    )


def test_default_selects_closest_name(qtbot):
    files = [_f(11, "StandardBody.zip"), _f(12, "AlternateBody.zip")]
    dlg = ModUpdateFileDialog(files, local_original_name="AlternateBody", parent=None)
    qtbot.addWidget(dlg)
    assert dlg.selected_file().file_id == 12


def test_returns_chosen(qtbot):
    files = [_f(11, "A.zip"), _f(12, "B.zip")]
    dlg = ModUpdateFileDialog(files, local_original_name="A", parent=None)
    qtbot.addWidget(dlg)
    dlg._list.setCurrentRow(1)
    assert dlg.selected_file().file_id == 12
