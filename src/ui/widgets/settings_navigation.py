"""设置主题导航及真实控件搜索。"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (
    QBoxLayout,
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLayout,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from src.ui import anim, theme
from src.ui.smooth_scroll import SmoothScroller
from src.ui.widgets.settings_components import icon_label, plain_widget, text_label
from src.utils.i18n import t as tr


class _TopicStack(QStackedWidget):
    """允许设置主题内容收缩到父布局可用区域的堆叠容器。"""

    def minimumSizeHint(self):
        # 长主题列表独立滚动，不扩大所有分类。
        """返回零最小尺寸，避免隐藏页面的自然尺寸撑大外层窗口。"""
        return QSize(0, 0)


class _SectionButton(QPushButton):
    """为当前设置小节额外绘制左侧强调标记的导航按钮。"""

    def paintEvent(self, event):
        """完成默认按钮绘制后，为选中小节绘制主题色竖向标记。"""
        super().paintEvent(event)
        if self.isChecked():
            painter = QPainter(self)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(theme.color("settings.accent")))
            painter.drawRoundedRect(QRectF(4, (self.height() - 18) / 2, 3, 18), 1.5, 1.5)


class SettingsCategoryPage(QWidget):
    """在单个设置大类内组织小节导航、独立滚动区域与目标控件定位。"""

    def __init__(self, category: str, sections: list[tuple[str, str, str, list[QWidget]]]):
        """按小节定义创建互斥导航与滚动内容页，注册控件所属关系并选中首个小节。"""
        super().__init__()
        self.setObjectName("settingsCategory")
        self.category = category
        self.section_titles = {key: title for key, title, _description, _widgets in sections}
        self._keys = list(self.section_titles)
        self._topic_widgets = {}
        self._scrolls = {}
        self._section_buttons = {}
        self._compact = None
        self._layout = QBoxLayout(QBoxLayout.Direction.LeftToRight, self)
        self._layout.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        self._layout.setContentsMargins(16, 16, 16, 12)
        self._layout.setSpacing(16)
        self._nav = plain_widget()
        self._nav.setFixedWidth(180)
        self._nav_layout = QBoxLayout(QBoxLayout.Direction.TopToBottom, self._nav)
        self._nav_layout.setContentsMargins(4, 0, 4, 0)
        self._nav_layout.setSpacing(4)
        group = QButtonGroup(self)
        self._topics = _TopicStack()
        for key, title, description, widgets in sections:
            button = _SectionButton(title)
            button.setObjectName("settingsSectionButton")
            button.setAccessibleName(title)
            button.setToolTip(description)
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda _checked, target=key: self.select_section(target))
            group.addButton(button)
            self._section_buttons[key] = button
            self._nav_layout.addWidget(button)
            content = QWidget()
            content.setObjectName("settingsTopic")
            content.setAccessibleName(f"{category} · {title}")
            content.setAccessibleDescription(description)
            content.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            outer = QVBoxLayout(content)
            outer.setContentsMargins(0, 0, 0, 12)
            outer.setSpacing(16)
            body = plain_widget()
            body.setMaximumWidth(1190)
            layout = QVBoxLayout(body)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(16)
            for widget in widgets:
                layout.addWidget(widget)
            layout.addStretch(1)
            outer.addWidget(body)
            outer.addStretch(1)
            scroll = QScrollArea()
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            scroll.setWidgetResizable(True)
            scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            scroll.setWidget(content)
            SmoothScroller.attach(scroll)
            self._topics.addWidget(scroll)
            self._topic_widgets[key] = (content, widgets)
            self._scrolls[key] = scroll
        self._nav_layout.addStretch(1)
        self._layout.addWidget(self._nav)
        self._layout.addWidget(self._topics, 1)
        self.select_section(self._keys[0])

    def select_section(self, key: str) -> None:
        """对有效小节切换内容并同步导航选中状态，跨页面切换时执行交叉淡入淡出。"""
        if key not in self._section_buttons:
            return
        index = self._keys.index(key)
        if self._topics.currentIndex() != index:
            anim.cross_fade(self._topics, lambda: self._topics.setCurrentIndex(index))
        self._section_buttons[key].setChecked(True)

    def current_section(self) -> str:
        """返回当前显示小节的稳定标识。"""
        return self._keys[self._topics.currentIndex()]

    def section_for(self, widget: QWidget) -> str | None:
        """按控件祖先关系查找所属小节，控件不属于本设置页时返回 None。"""
        for key, (content, _widgets) in self._topic_widgets.items():
            if widget is content or content.isAncestorOf(widget):
                return key
        return None

    def reveal(self, widget: QWidget) -> None:
        # 依赖设置隐藏目标时显示其所属可见面板，不擅自更改设置。
        """向上寻找目标最近的可见祖先，并滚动当前小节使其处于可视区域内。"""
        target = widget
        while target is not None and not target.isVisible():
            target = target.parentWidget()
        if target is not None:
            self._scrolls[self.current_section()].ensureWidgetVisible(target, 20, 24)

    def resizeEvent(self, event):
        """窄窗口中将小节导航移到内容上方，宽窗口中恢复左侧固定宽度导航。"""
        super().resizeEvent(event)
        compact = self.width() < 760
        if compact == self._compact:
            return
        self._compact = compact
        self._layout.setDirection(QBoxLayout.Direction.TopToBottom if compact else QBoxLayout.Direction.LeftToRight)
        self._nav_layout.setDirection(QBoxLayout.Direction.LeftToRight if compact else QBoxLayout.Direction.TopToBottom)
        self._nav_layout.setContentsMargins(0, 0, 0, 0) if compact else self._nav_layout.setContentsMargins(4, 0, 4, 0)
        self._nav.setMinimumWidth(0 if compact else 180)
        self._nav.setMaximumWidth(16777215 if compact else 180)
        self._nav.setMaximumHeight(54 if compact else 16777215)
        # 转场可能早于布局事件截图，尺寸变化的同一轮完成导航和内容分配。
        self._layout.activate()


@dataclass
class SettingSearchEntry:
    """保存设置搜索结果的分类位置、小节标识、展示文字、关键词及目标控件。"""

    category: int
    section: str
    title: str
    context: str
    keywords: str
    target: QWidget


class SettingsSearchPanel(QScrollArea):
    """展示设置关键词搜索结果，并通过信号请求定位到对应控件。"""

    activated = Signal(int, str, object)

    def __init__(self):
        """创建无横向滚动条的搜索结果区域、结果摘要和垂直条目布局。"""
        super().__init__()
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        host = plain_widget()
        outer = QVBoxLayout(host)
        outer.setContentsMargins(30, 22, 30, 24)
        self._summary = text_label("", "subtitle")
        outer.addWidget(self._summary)
        self._results = QVBoxLayout()
        self._results.setSpacing(9)
        outer.addLayout(self._results)
        outer.addStretch(1)
        self.setWidget(host)
        SmoothScroller.attach(self)

    def search(self, query: str, entries: list[SettingSearchEntry]) -> None:
        """移除旧结果，按所有空白分词均匹配的规则检索设置关键词并重建可点击结果。

        每个结果绑定其分类、小节和目标控件，更新完成后将滚动位置归零。
        """
        while self._results.count():
            item = self._results.takeAt(0)
            if item.widget() is not None:
                item.widget().hide()
                item.widget().deleteLater()
        terms = query.casefold().split()
        found = [entry for entry in entries if all(term in entry.keywords.casefold() for term in terms)]
        self._summary.setText(
            tr("找到 {count} 项相关设置", count=len(found))
            if found
            else tr("没有找到相关设置，试试“缓存”“代理”或“字体”。")
        )
        for entry in found:
            button = QPushButton()
            button.setObjectName("settingsSearchResult")
            button.setAccessibleName(f"{entry.title} · {entry.context}")
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setMaximumWidth(930)
            button.setMinimumHeight(68)
            layout = QHBoxLayout(button)
            layout.setContentsMargins(18, 14, 18, 14)
            layout.setSpacing(14)
            layout.addWidget(icon_label("settings", 18, "settings.muted"))
            words = QVBoxLayout()
            for title, role in ((entry.title, "label"), (entry.context, "muted")):
                label = text_label(title, role, wrap=True)
                label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
                words.addWidget(label)
            layout.addLayout(words, 1)
            layout.addWidget(icon_label("chevron", 14, "settings.muted"))
            button.clicked.connect(
                lambda _checked, result=entry: self.activated.emit(result.category, result.section, result.target)
            )
            self._results.addWidget(button)
        self.verticalScrollBar().setValue(0)
