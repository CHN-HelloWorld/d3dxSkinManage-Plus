from src.services.gamebanana.html_sanitizer import (
    ALLOWED_PROTOCOLS,
    ALLOWED_TAGS,
    sanitize_html,
    theme_css,
)


def test_script_tag_removed():
    out = sanitize_html("<p>ok</p><script>alert(1)</script>")
    assert "<script>" not in out
    assert "alert" not in out
    assert "<p>ok</p>" in out


def test_event_handler_removed():
    out = sanitize_html('<a href="https://ok" onclick="x()">ok</a>')
    assert "onclick" not in out
    assert 'href="https://ok"' in out


def test_javascript_protocol_blocked():
    out = sanitize_html('<a href="javascript:alert(1)">x</a>')
    assert "javascript:" not in out


def test_data_protocol_blocked():
    out = sanitize_html('<img src="data:image/png;base64,abc">')
    assert "data:" not in out


def test_safe_image_preserved():
    src = "https://images.gamebanana.com/img/a.png"
    out = sanitize_html(f'<img src="{src}">')
    assert src in out


def test_video_replaced_with_placeholder():
    out = sanitize_html('<video src="x.mp4"></video>')
    assert "<video" not in out
    # 无法展示的视频替换为可见占位内容。
    assert "video" in out.lower() or "[视频" in out or "<p" in out


def test_dark_theme_css_available():
    # 正文仅包含内容；主题 CSS 由视图通过 setDefaultStyleSheet 应用。
    css = theme_css("dark")
    assert ".RedColor" in css and "#FF4E4E" in css
    assert sanitize_html("<p>x</p>", theme="dark") == "<p>x</p>"


def test_light_theme_css_available():
    css = theme_css("light")
    assert ".GreenColor" in css and "#6EE16C" in css
    assert "<style" not in css  # 正文不包含 style 标签。


def test_theme_css_has_gamebanana_color_classes():
    css = theme_css("dark")
    for cls, hexv in (
        (".RedColor", "#FF4E4E"),
        (".GreenColor", "#6EE16C"),
        (".BlueColor", "#6CB1E1"),
        (".OrangeColor", "#FF7238"),
        (".PurpleColor", "#FF5E9D"),
        (".GreyColor", "#999999"),
    ):
        assert cls in css and hexv in css


def test_legacy_malformed_font_color_normalized():
    # 浏览器的 HTML 颜色解析规则将五位十六进制颜色 #6ECFF 补齐为 #6ECFF0。
    out = sanitize_html('<font color="#6ECFF">Thanks</font>')
    assert 'color="#6ECFF0"' in out
    assert '#6ECFF"' not in out  # 畸形值不应原样残留


def test_valid_font_color_unchanged():
    out = sanitize_html('<font color="#6EC6FF">x</font>')
    assert 'color="#6EC6FF"' in out


def test_color_class_span_preserved():
    # mods/689309：<span class="RedColor"> 的 class 必须保留（配合 theme_css 的 .RedColor）。
    out = sanitize_html('<span class="RedColor">warn</span>')
    assert 'class="RedColor"' in out


def test_inline_style_color_preserved():
    # CSS 清理应保留允许的行内颜色属性。
    out = sanitize_html('<span style="color:#123456">x</span>')
    assert "#123456" in out


def test_changelog_tags_get_class():
    out = sanitize_html('<span class="tag-bugfix">fix</span>')
    assert "tag-bugfix" in out


def test_table_kept():
    out = sanitize_html("<table><tr><td>a</td></tr></table>")
    assert "<table" in out
    assert "<td" in out


def test_allowed_tags_contains_basics():
    for tag in ("p", "a", "img", "b", "i", "strong", "em", "ul", "ol", "li", "table"):
        assert tag in ALLOWED_TAGS


def test_allowed_protocols_https():
    assert "https" in ALLOWED_PROTOCOLS
    assert "javascript" not in ALLOWED_PROTOCOLS


def test_legacy_font_tag_preserved():
    # GameBanana 描述可用 font 标签指定字号与颜色，清理后应保留这些受支持的属性。
    out = sanitize_html('<font size="6" color="#b388ff">Toggles</font>')
    assert "<font" in out
    assert 'size="6"' in out
    assert 'color="#b388ff"' in out
    assert "Toggles" in out


def test_legacy_center_tag_preserved():
    out = sanitize_html("<center>hi</center>")
    assert "<center>" in out and "</center>" in out


def test_font_and_linkify_coexist():
    # bleach.linkify 会转义 font 和 center 标签，处理后应还原允许的标签。
    # 同一段落中的 font 标签和普通链接均应保留。
    out = sanitize_html('<font color="#64b5f6">see https://gamebanana.com</font>')
    assert '<font color="#64b5f6">' in out
    assert '<a href="https://gamebanana.com"' in out


def test_font_cannot_carry_event_handler():
    # 还原 font/center 不能把危险属性带回来：onclick 必须已被 clean 去掉。
    out = sanitize_html('<font color="red" onclick="evil()">x</font>')
    assert "onclick" not in out
    assert "<font" in out and 'color="red"' in out


def test_script_inside_font_removed():
    out = sanitize_html('<font size="5"><script>alert(1)</script>ok</font>')
    assert "<script" not in out
    assert "alert" not in out
    assert "ok" in out
