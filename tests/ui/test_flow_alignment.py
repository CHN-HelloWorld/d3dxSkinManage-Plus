from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QPushButton, QWidget

from src.ui.widgets.tag_chip import TagFlowLayout


def test_centered_flow_reflows_each_visible_row(qtbot):
    host = QWidget()
    qtbot.addWidget(host)
    layout = TagFlowLayout(host, margin=11, spacing=6)
    layout.setAlignment(Qt.AlignmentFlag.AlignHCenter)
    buttons = []
    for width in (80, 95, 100):
        button = QPushButton("Action")
        button.setFixedSize(width, 32)
        layout.addWidget(button)
        buttons.append(button)
    host.resize(240, 120)
    host.show()
    QApplication.processEvents()

    assert [button.geometry().getRect() for button in buttons] == [
        (29, 11, 80, 32),
        (115, 11, 95, 32),
        (70, 49, 100, 32),
    ]
    assert layout.heightForWidth(240) == 92

    host.resize(340, 120)
    QApplication.processEvents()
    assert [button.geometry().getRect() for button in buttons] == [
        (26, 11, 80, 32),
        (112, 11, 95, 32),
        (213, 11, 100, 32),
    ]
    assert layout.heightForWidth(340) == 54

    buttons[0].hide()
    QApplication.processEvents()
    assert [button.geometry().getRect() for button in buttons[1:]] == [
        (69, 11, 95, 32),
        (170, 11, 100, 32),
    ]
