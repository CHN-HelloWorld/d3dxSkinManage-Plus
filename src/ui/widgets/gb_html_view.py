"""清理并渲染 HTML 内容，按需加载远程图片。"""

from __future__ import annotations

import re
from typing import Literal

from PySide6.QtCore import Qt, QTimer, QUrl, Slot
from PySide6.QtGui import QTextDocument
from PySide6.QtWidgets import QLabel, QTextBrowser

from src.services.gamebanana.cache import ImageCache
from src.services.gamebanana.html_sanitizer import sanitize_html, theme_css

_IMG_RE = re.compile(r'<img[^>]+src="([^"]+)"', re.IGNORECASE)


class GbHtmlView(QTextBrowser):
    """支持内容清理和远程图片加载的富文本视图。"""

    def __init__(
        self,
        *,
        image_loader,
        image_cache: ImageCache | None = None,
        theme: Literal["dark", "light"] = "dark",
        parent=None,
    ) -> None:
        """绑定图片加载与缓存服务，复用图片轮询及合并重排定时器，并设置富文本主题。"""
        super().__init__(parent)
        self._loader = image_loader
        self._cache = image_cache
        self._theme = theme
        self._html_raw = ""
        # 本视图持有远程图像的 QLabel，按 URL 收集结果并合并触发 HTML 重绘。
        self._img_targets: dict[str, QLabel] = {}
        self._spare_targets: list[QLabel] = []
        self._image_generation = 0
        self._img_resources: dict[str, object] = {}  # url -> QImage
        self._pending_images: dict[str, int] = {}
        self._image_poll_timer = QTimer(self)
        self._image_poll_timer.setInterval(60)
        self._image_poll_timer.timeout.connect(self._poll_images)
        self._rerender_pending = False
        self._rerender_timer = QTimer(self)
        self._rerender_timer.setSingleShot(True)
        self._rerender_timer.setInterval(120)
        self._rerender_timer.timeout.connect(self._rerender_with_images)
        self.setOpenExternalLinks(True)
        # 描述和更新区域禁用 QTextBrowser 默认文本菜单。
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        # 显式设置文档默认字号，避免全局控件字号改变正文尺寸。
        _f = self.font()
        _f.setPixelSize(14)
        self.document().setDefaultFont(_f)
        # 通过文档默认样式表应用描述样式，并在 setHtml 前设置，
        # 保证类、元素及标题选择器按文档规则解析。
        self.document().setDefaultStyleSheet(theme_css(theme))

    def set_theme(self, theme: Literal["dark", "light"]) -> None:
        """更新文档主题样式，并对已有原始 HTML 重新清洗和加载图片。"""
        self._theme = theme
        self.document().setDefaultStyleSheet(theme_css(theme))
        if self._html_raw:
            self.set_html(self._html_raw)

    def set_html(self, html: str) -> None:
        # 内容切换时清理上一轮图片及重绘状态，防止旧请求结果进入新文档。
        """保存原始内容、清空图片加载状态，清洗 HTML 后显示并请求其中的网络图片。"""
        self._html_raw = html or ""
        self._image_generation += 1
        self._image_poll_timer.stop()
        self._pending_images.clear()
        self._spare_targets.extend(self._img_targets.values())
        self._img_targets.clear()
        for target in self._spare_targets:
            self._loader.cancel_for(target)
            target.clear()
        self._img_resources.clear()
        self._rerender_pending = False
        self._rerender_timer.stop()
        cleaned = sanitize_html(self._html_raw, theme=self._theme)
        # setHtml 保留手工加入的文档资源，切换内容前须显式清除上一页图片。
        self.document().clear()
        self.setHtml(cleaned)
        self._fetch_all_images()
        # 复用当前页所需的标签；额外只保留一个空标签，不让历史大文档扩大空闲池。
        while len(self._spare_targets) > 1:
            self._spare_targets.pop().deleteLater()

    def _fetch_all_images(self) -> None:
        """提取原始 HTML 中去重后的 HTTP 或 HTTPS 图片地址并启动加载。"""
        urls = set(_IMG_RE.findall(self._html_raw or ""))
        for url in urls:
            if not url.startswith(("http://", "https://")):
                continue
            self._loader_for_url(url)

    def _loader_for_url(self, url: str) -> None:
        """异步获取远程图像，由视图持有承载 QLabel。

        收到图像后合并触发重绘，避免逐张重建活动文档与页面截图交错访问 QTextDocument。
        """
        if url in self._img_targets:
            return  # 已在抓取
        target = self._spare_targets.pop() if self._spare_targets else QLabel(self)
        target.hide()
        self._img_targets[url] = target
        # 轮询图像结果，完成或超时后合并重绘。
        self._loader.load(url, target)
        self._pending_images[url] = 0
        if not self._image_poll_timer.isActive():
            self._image_poll_timer.start()

    @Slot()
    def _poll_images(self) -> None:
        """一个定时器收集当前文档的待完成图片，避免每张图片反复创建定时器和闭包。"""
        generation = self._image_generation
        for url, tries in tuple(self._pending_images.items()):
            self._collect_image(url, tries, generation=generation)

    def _collect_image(self, url: str, _tries: int = 0, *, generation: int | None = None) -> None:
        """轮询图片加载目标，取得有效像素图后注册文档资源并合并重排请求。

        每张图最多继续尝试三十二次；目标已移除或 Qt 对象失效时结束轮询。
        """
        if generation is not None and generation != self._image_generation:
            return
        self._pending_images.pop(url, None)
        try:
            target = self._img_targets.get(url)
            if target is None:
                return
            try:
                pm = target.pixmap()
            except RuntimeError:
                return
            if pm is not None and not pm.isNull():
                self._img_resources[url] = pm.toImage()
                self._schedule_rerender()
            elif _tries < 32:
                self._pending_images[url] = _tries + 1
        finally:
            if not self._pending_images:
                self._image_poll_timer.stop()
            elif not self._image_poll_timer.isActive():
                self._image_poll_timer.start()

    def _schedule_rerender(self) -> None:
        """合并图片完成通知，仅触发一次文档重绘。"""
        self._rerender_pending = True
        self._rerender_timer.start()

    def _rerender_with_images(self) -> None:
        """将已获取图片写入文档资源，再设置清理后的 HTML。

        使用保存的 HTML 字符串，避免序列化活动文档后重新解析造成内容变化。
        """
        if not self._rerender_pending:
            return
        self._rerender_pending = False
        doc = self.document()
        try:
            for url, image in self._img_resources.items():
                doc.addResource(
                    QTextDocument.ResourceType.ImageResource,
                    QUrl(url),
                    image,
                )
            cleaned = sanitize_html(self._html_raw, theme=self._theme)
            self.setHtml(cleaned)
        except RuntimeError:
            # 文档正在销毁时停止更新。
            pass
