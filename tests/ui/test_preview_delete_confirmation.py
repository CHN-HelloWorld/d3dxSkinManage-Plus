import re
from pathlib import Path

import pytest
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QMessageBox

from src.models.mod import Mod
from src.ui.pages.overview import OverviewPage


@pytest.mark.parametrize("delete_all", [False, True])
@pytest.mark.parametrize("choice", ["yes", "no", "close"])
@pytest.mark.parametrize("language", ["zh-CN", "en-US"])
def test_preview_delete_requires_confirmation(
    qtbot,
    qapp,
    temp_data_dir,
    native_qt_teardown,
    tmp_path,
    monkeypatch,
    delete_all,
    choice,
    language,
):
    from src.utils import i18n

    catalog = i18n.I18n(directory=Path(__file__).resolve().parents[2] / "config/languages")
    assert catalog.set_language(language)
    monkeypatch.setattr(i18n, "_i18n", catalog)
    paths = []
    for name in ("first.png", "second.png"):
        path = str(tmp_path / name)
        image = QPixmap(32, 32)
        image.fill(Qt.GlobalColor.red)
        assert image.save(path)
        paths.append(path)

    page = OverviewPage()
    qtbot.addWidget(page)
    mod = Mod(
        mod_id="preview-mod",
        game_id="game_a",
        name="Preview Mod",
        folder_name="Preview Mod",
        path="",
        imported_at="2026-10-25T10:00:00",
        preview_images=paths,
    )
    page._selected_mod = mod
    page._show_mod_panel(mod)
    # 展示顺序可能与 Mod 保存的顺序不同。
    page._panel_preview.set_images(list(reversed(paths)))
    requests = []
    page.mod_preview_delete_requested.connect(lambda mod_id, path: requests.append((mod_id, path)))
    page.mod_preview_delete_all_requested.connect(lambda mod_id: requests.append((mod_id,)))

    confirmations = []

    def respond():
        box = qapp.activeModalWidget()
        assert isinstance(box, QMessageBox)
        try:
            confirmations.append(True)
            assert requests == []
            assert box.standardButton(box.defaultButton()) == QMessageBox.StandardButton.No
            if language == "en-US":
                assert not re.search(r"[\u3400-\u9fff]", box.windowTitle() + box.text())
            if choice == "close":
                box.close()
            else:
                answer = QMessageBox.StandardButton.Yes if choice == "yes" else QMessageBox.StandardButton.No
                box.button(answer).click()
        finally:
            if box.isVisible():
                box.reject()

    # 静态消息框由 C++ 持有，须在模态执行期间获取。
    QTimer.singleShot(0, respond)
    button = page._panel_preview._delete_all_btn if delete_all else page._panel_preview._delete_btn
    button.click()

    expected = ("preview-mod",) if delete_all else ("preview-mod", paths[1])
    assert confirmations == [True]
    assert requests == ([expected] if choice == "yes" else [])
