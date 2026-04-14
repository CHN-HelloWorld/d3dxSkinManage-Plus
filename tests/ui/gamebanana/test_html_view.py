from src.ui.widgets.gb_html_view import GbHtmlView


def test_set_html_sanitizes(qtbot, fake_image_loader):
    v = GbHtmlView(image_loader=fake_image_loader)
    qtbot.addWidget(v)
    v.set_html("<p>ok</p><script>alert(1)</script>")
    text = v.toHtml()
    assert "alert" not in text
    assert "ok" in text


def test_remote_image_uses_loader(qtbot, fake_image_loader, monkeypatch):
    v = GbHtmlView(image_loader=fake_image_loader)
    qtbot.addWidget(v)
    called = []
    monkeypatch.setattr(fake_image_loader, "load", lambda url, target, **kw: called.append(url))
    v.set_html('<img src="https://images.gamebanana.com/a.png">')
    v._fetch_all_images()
    assert any("a.png" in u for u in called)


def test_theme_switching(qtbot, fake_image_loader):
    v = GbHtmlView(image_loader=fake_image_loader, theme="dark")
    qtbot.addWidget(v)
    v.set_html("<p>x</p>")
    v.set_theme("light")
    v.set_html("<p>y</p>")
    assert "y" in v.toHtml()
