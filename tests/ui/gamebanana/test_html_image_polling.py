"""描述图复用轮询定时器，内容替换与超时后不保留旧轮询。"""

from __future__ import annotations

import weakref

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QPixmap

from src.ui.widgets.gb_html_view import GbHtmlView


class _PendingImages:
    """保留可控的图片请求，交付时仍通过真实标签及文档更新流程。"""

    def __init__(self):
        """按 URL 弱引用当前接收标签，避免加载器替视图延长控件寿命。"""
        self.targets = {}

    def load(self, url, target, **kwargs):
        """记录请求，暂不返回像素以模拟异步读取。"""
        self.targets[url] = weakref.ref(target)

    def cancel_for(self, target):
        """取消指定标签的旧绑定，使后续交付不会写入已复用的标签。"""
        self.targets = {url: ref for url, ref in self.targets.items() if ref() is not target}

    def deliver(self, url):
        """向仍有效的当前请求交付合成像素。"""
        target = self.targets[url]()
        pixmap = QPixmap(16, 16)
        pixmap.fill(Qt.GlobalColor.blue)
        target.setPixmap(pixmap)


def test_many_description_images_reuse_the_same_polling_timer(qtbot):
    loader = _PendingImages()
    view = GbHtmlView(image_loader=loader)
    qtbot.addWidget(view)
    view.set_html('<img src="https://example.invalid/first.png">')
    timers = set(view.findChildren(QTimer))
    urls = [f"https://example.invalid/current-{number}.png" for number in range(30)]
    view.set_html("".join(f'<img src="{url}">' for url in urls))
    assert set(view.findChildren(QTimer)) == timers, "more images must not allocate more polling timers"
    for url in urls:
        loader.deliver(url)
    qtbot.waitUntil(lambda: set(view._img_resources) == set(urls))
    view.set_html("<p>empty</p>")
    assert set(view.findChildren(QTimer)) == timers
    assert all(not timer.isActive() for timer in timers)


def test_replaced_description_cannot_poll_or_install_its_previous_images(qtbot):
    loader = _PendingImages()
    view = GbHtmlView(image_loader=loader)
    qtbot.addWidget(view)
    old = "https://example.invalid/old.png"
    current = "https://example.invalid/current.png"
    view.set_html(f'<img src="{old}">')
    generation = view._image_generation
    view.set_html(f'<img src="{current}">')
    view._collect_image(old, generation=generation)
    view._collect_image(current, generation=generation)
    assert view._img_resources == {}
    loader.deliver(current)
    qtbot.waitUntil(lambda: current in view._img_resources)
    assert set(view._img_resources) == {current}
    assert old not in loader.targets


def test_unavailable_images_eventually_stop_the_reused_timer(qtbot):
    view = GbHtmlView(image_loader=_PendingImages())
    qtbot.addWidget(view)
    view.set_html('<img src="https://example.invalid/unavailable.png">')
    timers = set(view.findChildren(QTimer))
    for _ in range(100):
        view._poll_images()
    assert set(view.findChildren(QTimer)) == timers
    assert all(not timer.isActive() for timer in timers)
    assert view._img_resources == {}
