from types import SimpleNamespace

import pytest
import shiboken6
from PySide6.QtWidgets import QWidget

from src.services.gamebanana.models import GbModFile
from src.services.gamebanana.update_checker import UpdateInfo
from src.ui.dialogs.mod_update_file_dialog import ModUpdateBatchFileDialog, ModUpdateFileDialog
from src.ui.main_window import MainWindow
from tests.qt_dialogs import interact
from tests.ui.test_transient_ui_reuse import _flush


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


@pytest.mark.parametrize(
    ("entrypoint", "dialog_type"),
    [("single", ModUpdateFileDialog), ("batch", ModUpdateBatchFileDialog)],
)
def test_update_entrypoints_consume_selection_then_release_picker(qapp, temp_data_dir, entrypoint, dialog_type):
    files = [_f(11, "A.zip"), _f(12, "B.zip")]
    info = UpdateInfo(
        local_mod_id="mod",
        gb_mod_id=1,
        current_version="1",
        current_file_ts=1,
        latest_file=files[0],
        latest_version="2",
        candidate_files=files,
        changelog=[],
        page_url="",
    )
    service = SimpleNamespace(
        active_update_ids=lambda: set(),
        updatable_ids=lambda: {"mod"},
        get_update_status=lambda: {"mod": info},
    )
    host = QWidget()
    host._mod_update_service = service
    host._mod_manager = SimpleNamespace(
        get_mod=lambda _mod_id: SimpleNamespace(folder_name="A"),
        current_game_id="game",
    )
    host._mod_updates_dialog = None
    selected = []
    host._start_mod_update = lambda mod_id, *, chosen_file=None: selected.append((mod_id, chosen_file))
    dialogs = []

    def accept(dialog):
        """记录真实选择器，保留默认选择并通过确认路径返回业务结果。"""
        dialogs.append(dialog)
        dialog.accept()

    try:
        for _ in range(4):
            with interact(dialog_type, accept):
                if entrypoint == "single":
                    MainWindow._on_apply_update(host, "mod")
                else:
                    MainWindow._on_update_all(host)
            _flush(qapp)

        assert selected == [("mod", files[0])] * 4
        assert not host.findChildren(dialog_type)
        assert all(not shiboken6.isValid(dialog) for dialog in dialogs)
    finally:
        host.deleteLater()
        _flush(qapp)
