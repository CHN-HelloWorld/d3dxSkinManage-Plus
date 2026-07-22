from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QComboBox, QSplitter, QVBoxLayout, QWidget

from src.ui import acrylic


def test_unparented_editable_combo_reparent_preserves_global_coordinates(qtbot, qapp):
    popup_filter = acrylic.AcrylicPopupFilter()
    qapp.installEventFilter(popup_filter)
    host = QWidget()
    host.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
    qtbot.addWidget(host)
    host.move(120, 80)
    host.resize(700, 450)
    splitter = QSplitter(host)
    splitter.move(30, 40)
    splitter.resize(600, 350)
    page = QWidget()
    splitter.addWidget(page)
    layout = QVBoxLayout(page)
    try:
        # 设置页先构造字体选择器，再将其加入表单。
        combo = QComboBox()
        combo.setEditable(True)
        combo.addItems(["Font one", "Font two"])
        editor = combo.lineEdit()
        assert not combo.testAttribute(Qt.WidgetAttribute.WA_NativeWindow)
        assert not editor.testAttribute(Qt.WidgetAttribute.WA_NativeWindow)
        layout.addWidget(combo)
        host.show()
        qapp.processEvents()
        for widget in (splitter, page, combo, editor):
            assert not widget.testAttribute(Qt.WidgetAttribute.WA_NativeWindow)
        expected = host.mapToGlobal(combo.mapTo(host, QPoint()))
        assert combo.mapToGlobal(QPoint()) == expected
        host.move(320, 180)
        qapp.processEvents()
        assert combo.mapToGlobal(QPoint()) == host.mapToGlobal(combo.mapTo(host, QPoint()))
    finally:
        qapp.removeEventFilter(popup_filter)
