import gc
import weakref

import pytest
import shiboken6
from PySide6.QtCore import QUrl
from PySide6.QtGui import QColor, QPixmap, QTextDocument


@pytest.mark.parametrize("destroy", [False, True])
def test_ready_font_callback_follows_catalog_lifetime(qapp, monkeypatch, destroy):
    from src.ui import fonts

    monkeypatch.setattr(fonts, "installed_families", lambda: [])
    catalog = fonts.FontCatalog(qapp)
    qapp.processEvents()
    assert catalog.is_ready
    calls = []
    catalog.when_ready(lambda: calls.append("ready"))
    assert calls == []
    if destroy:
        shiboken6.delete(catalog)
    qapp.processEvents()
    assert calls == ([] if destroy else ["ready"])
    if not destroy:
        shiboken6.delete(catalog)


class _LoadedImage:
    def load(self, url, target):
        pixels = QPixmap(4, 4)
        pixels.fill(QColor("blue"))
        target.setPixmap(pixels)


def test_pending_html_image_poll_releases_destroyed_view(qapp):
    from src.ui.widgets.gb_html_view import GbHtmlView

    view = GbHtmlView(image_loader=_LoadedImage())
    view._loader_for_url("https://example.invalid/test.png")
    reference = weakref.ref(view)
    shiboken6.delete(view)
    del view
    gc.collect()
    assert reference() is None, "a delayed image poll must not retain a deleted view"


def test_live_html_image_poll_still_delivers_pixels(qtbot):
    from src.ui.widgets.gb_html_view import GbHtmlView

    view = GbHtmlView(image_loader=_LoadedImage())
    qtbot.addWidget(view)
    url = "https://example.invalid/test.png"
    view.set_html(f'<p><img src="{url}"></p>')
    qtbot.waitUntil(lambda: view.document().resource(QTextDocument.ResourceType.ImageResource, QUrl(url)) is not None)
    image = view.document().resource(QTextDocument.ResourceType.ImageResource, QUrl(url))
    assert image.pixelColor(0, 0) == QColor("blue")
