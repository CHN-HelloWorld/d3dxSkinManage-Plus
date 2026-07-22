from pathlib import Path

import pytest
from PySide6.QtWidgets import QStyle, QStyleOptionComboBox

from src.ui import theme
from src.ui.dialogs.mod_import import ModImportDialog
from src.ui.pages.overview import OverviewPage
from src.utils import i18n


@pytest.fixture(params=["zh-CN", "en-US"])
def rating_language(request, qapp, monkeypatch, temp_data_dir):
    catalog = i18n.I18n(directory=Path(__file__).resolve().parents[2] / "config/languages")
    assert catalog.set_language(request.param)
    monkeypatch.setattr(i18n, "_i18n", catalog)
    previous = qapp.styleSheet()
    qapp.setStyleSheet(theme.render_qss("light"))
    yield
    qapp.setStyleSheet(previous)


def assert_text_fits(combo):
    option = QStyleOptionComboBox()
    combo.initStyleOption(option)
    text_rect = combo.style().subControlRect(
        QStyle.ComplexControl.CC_ComboBox,
        option,
        QStyle.SubControl.SC_ComboBoxEditField,
        combo,
    )
    assert text_rect.width() >= combo.fontMetrics().horizontalAdvance(combo.currentText())


def test_rating_filter_keeps_selected_description_readable(qtbot, rating_language):
    page = OverviewPage()
    qtbot.addWidget(page)
    page.resize(1200, 800)
    page._type_filter.setCurrentIndex(page._type_filter.findData("mod"))
    combo = page._age_filter
    combo.setCurrentIndex(combo.findData("X"))
    page.show()
    qtbot.waitUntil(combo.isVisible)

    assert combo.currentData() == "X"
    assert_text_fits(combo)


def test_import_rating_column_fits_descriptions_after_resizing(qtbot, rating_language, tmp_path):
    source = tmp_path / "Example"
    source.mkdir()
    (source / "mod.ini").write_text("[KeyExample]\nkey = x\n", encoding="utf-8")
    dialog = ModImportDialog(initial_paths=[str(source)])
    qtbot.addWidget(dialog)
    dialog.show()
    combo = dialog._table.cellWidget(0, 2)
    qtbot.waitUntil(combo.isVisible)

    for width in (900, 680, 1100):
        dialog.resize(width, dialog.height())
        for rating in ("X", "P"):
            combo.setCurrentIndex(combo.findData(rating))
            qtbot.wait(10)
            assert dialog._entries[0].age_rating == rating
            assert_text_fits(combo)
