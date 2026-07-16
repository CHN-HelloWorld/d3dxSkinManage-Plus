"""可键盘操作的静态短片入口，不加载媒体后端。"""

from __future__ import annotations

from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QImageReader, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QSizePolicy, QVBoxLayout, QWidget

from src.ui import icons, theme
from src.utils.i18n import t as tr
from src.utils.project_story import load_project_story


class _StoryCover(QWidget):
    """显示短片静态封面，缺少可读图像时绘制占位图案。"""

    def __init__(self, parent: QWidget) -> None:
        """建立可随卡片伸缩的封面区域，并让鼠标事件交由外层播放入口处理。"""
        super().__init__(parent)
        self._pixmap = QPixmap()
        self.setMinimumWidth(200)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def load(self, filename: str) -> None:
        """重新读取指定封面并限制大图解码尺寸，使同路径替换后的文件也能立即显示。"""
        # 显式读取像素，确保同名文件替换后再次打开页面能显示新内容。
        reader = QImageReader(filename)
        reader.setAutoTransform(True)
        size = reader.size()
        if size.isValid() and (size.width() > 1200 or size.height() > 800):
            reader.setScaledSize(size.scaled(QSize(1200, 800), Qt.AspectRatioMode.KeepAspectRatio))
        self._pixmap = QPixmap.fromImage(reader.read())
        self.update()

    def paintEvent(self, event) -> None:
        """在圆角区域内等比居中绘制封面，图像无效时使用渐变及图标占位。"""
        painter = QPainter(self)
        painter.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        clip = QPainterPath()
        clip.addRoundedRect(QRectF(self.rect()), 10, 10)
        painter.setClipPath(clip)
        painter.fillRect(self.rect(), QColor("#0b121d"))
        if not self._pixmap.isNull():
            size = self._pixmap.size().scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio)
            target = QRectF(
                (self.width() - size.width()) / 2, (self.height() - size.height()) / 2, size.width(), size.height()
            )
            painter.drawPixmap(target, self._pixmap, QRectF(self._pixmap.rect()))
        else:
            gradient = QLinearGradient(0, 0, self.width(), self.height())
            gradient.setColorAt(0, QColor("#102a40"))
            gradient.setColorAt(1, QColor("#241e16"))
            painter.fillRect(self.rect(), gradient)
            spark = icons.colored_pixmap_from("meta/spark", 72, "#f2c84b")
            painter.drawPixmap((self.width() - 72) // 2, (self.height() - 72) // 2, spark)
        painter.end()


class ProjectStoryCard(QPushButton):
    """提供短片标题、封面和无障碍描述，通过按钮激活事件交由页面开启播放。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        """创建可键盘聚焦的短片入口，布置封面和文字并加载当前短片信息。"""
        super().__init__(parent)
        self.setObjectName("projectStoryCard")
        self.setAutoDefault(False)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMinimumHeight(244)
        self.setStyleSheet("QPushButton#projectStoryCard { min-height: 244px; padding: 0; border: none; }")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 14, 26, 14)
        layout.setSpacing(28)
        self.cover = _StoryCover(self)
        layout.addWidget(self.cover, 11)
        info = QWidget(self)
        info.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        info.setStyleSheet("background: transparent;")
        details = QVBoxLayout(info)
        details.setContentsMargins(0, 8, 0, 8)
        details.setSpacing(8)
        accent = "#60cdff" if theme.current_theme() == "dark" else "#0067c0"
        kicker = self._label(tr("项目故事"), 12, accent, bold=True)
        details.addWidget(kicker)
        self.title_label = self._label("", 28, theme.color("text.primary"), bold=True)
        details.addWidget(self.title_label)
        self.subtitle_label = self._label("", 14, theme.color("text.secondary"))
        details.addWidget(self.subtitle_label)
        self.edition_label = self._label("", 12, theme.color("text.tertiary"))
        details.addWidget(self.edition_label)
        details.addStretch(1)
        action = QHBoxLayout()
        action.setSpacing(8)
        icon = QLabel(info)
        icon.setPixmap(icons.colored_pixmap_from("actions/play", 20, accent))
        icon.setFixedSize(20, 20)
        action.addWidget(icon)
        play_label = self._label(tr("播放短片"), 14, accent, bold=True)
        play_label.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Preferred)
        action.addWidget(play_label)
        action.addStretch(1)
        details.addLayout(action)
        layout.addWidget(info, 10)
        self.reload()

    @staticmethod
    def _label(text: str, pixels: int, color: str, *, bold: bool = False) -> QLabel:
        """创建按像素字号显示的纯文本标签，允许换行并适应卡片宽度。"""
        label = QLabel(text)
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setWordWrap(True)
        label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        font = QFont()
        font.setPixelSize(pixels)
        if bold:
            font.setWeight(QFont.Weight.DemiBold)
        label.setFont(font)
        label.setStyleSheet(
            f"color: {color}; background: transparent; font-size: {pixels}px;font-weight: {600 if bold else 400};"
        )
        return label

    def reload(self) -> None:
        """重新加载短片元信息、封面及翻译文案，同步无障碍名称和描述。"""
        self.story = load_project_story()
        self.title_label.setText(tr(self.story.title))
        self.subtitle_label.setText(tr(self.story.subtitle))
        self.edition_label.setText(tr(self.story.edition))
        self.cover.load(str(self.story.cover_path))
        self.setAccessibleName(tr("播放短片") + " · " + tr(self.story.title))
        self.setAccessibleDescription(tr(self.story.subtitle))

    def sizeHint(self) -> QSize:
        """提供适合横向排列封面和说明文字的推荐卡片尺寸。"""
        return QSize(840, 250)

    def minimumSizeHint(self) -> QSize:
        """为窄窗口布局提供卡片的最小建议尺寸。"""
        return QSize(440, 244)

    def paintEvent(self, event) -> None:
        """按按压、悬停和键盘焦点状态绘制圆角卡片及边框，并适配背景透明度。"""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        background = QColor(theme.color("bg.hover" if self.isDown() else "bg.card"))
        if theme.is_background_active():
            background.setAlpha(210)
        painter.setBrush(background)
        accent = "#60cdff" if theme.current_theme() == "dark" else "#0067c0"
        border = accent if self.underMouse() or self.hasFocus() else theme.color("stroke.control")
        painter.setPen(QPen(QColor(border), 1))
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 14, 14)
        painter.end()
