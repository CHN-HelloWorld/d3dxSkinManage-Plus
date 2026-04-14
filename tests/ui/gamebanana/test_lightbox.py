from PySide6.QtCore import Qt

from src.ui.widgets.gb_lightbox import GbLightBox


def test_open_displays_progress(qtbot, fake_image_loader):
    lb = GbLightBox(image_loader=fake_image_loader)
    qtbot.addWidget(lb)
    lb.show_for_url("https://x.com/a.png")
    assert lb.progress is not None


def test_esc_closes(qtbot, fake_image_loader):
    lb = GbLightBox(image_loader=fake_image_loader)
    qtbot.addWidget(lb)
    lb.show_for_url("https://x.com/a.png")
    qtbot.keyClick(lb, Qt.Key.Key_Escape)
    qtbot.wait(100)
    assert not lb.isVisible()
