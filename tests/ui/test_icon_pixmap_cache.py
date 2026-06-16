from PySide6.QtGui import QColor

from src.ui import icons


def test_pixmap_reuses_render_and_returns_independent_copy(qapp, monkeypatch):
    icons.clear_cache()
    original = icons._render_pixmap
    renders = []

    def render(svg, size):
        renders.append(size)
        return original(svg, size)

    monkeypatch.setattr(icons, "_render_pixmap", render)
    first = icons.colored_pixmap_from("actions/mod", 16, "#ffffff")
    expected = first.toImage()
    first.fill(QColor("red"))
    second = icons.colored_pixmap_from("actions/mod", 16, "#ffffff")
    assert second.toImage() == expected
    assert renders == [16]
    assert icons.colored_pixmap_from("actions/mod", 24, "#ffffff").width() == 24
    assert icons.colored_pixmap_from("actions/mod", 16, "#ff0000").toImage() != expected
    assert renders == [16, 24, 16]
    icons.clear_cache()
    icons.colored_pixmap_from("actions/mod", 16, "#ffffff")
    assert renders == [16, 24, 16, 16]


def test_theme_pixmap_shares_render_for_resolved_color(qapp, monkeypatch):
    icons.clear_cache()
    first = icons.pixmap("actions/mod", 16)
    monkeypatch.setattr(
        icons, "_render_pixmap", lambda *args: (_ for _ in ()).throw(AssertionError("re-rendered cached icon"))
    )
    assert icons.pixmap("actions/mod", 16).toImage() == first.toImage()
