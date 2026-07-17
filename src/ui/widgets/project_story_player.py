"""页内短片播放器，可使用现有应用窗口全屏显示。"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QRectF, QSignalBlocker, QSize, Qt, QUrl, Signal
from PySide6.QtGui import QColor, QIcon, QImage, QImageReader, QKeySequence, QPainter, QPainterPath, QShortcut
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer, QVideoSink
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QSlider,
    QStyle,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from src.ui import icons, theme
from src.utils.i18n import t as tr
from src.utils.project_story import ProjectStory


def _clock(milliseconds: int) -> str:
    """将非负毫秒时长格式化为分秒，达到一小时后增加小时字段。"""
    seconds = max(0, milliseconds) // 1000
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02}:{seconds:02}" if hours else f"{minutes:02}:{seconds:02}"


class _VideoSurface(QWidget):
    """在控件树内绘制，供透明窗口和滚动合成。"""

    clicked = Signal()

    def __init__(self, cover_path, parent: QWidget) -> None:
        """加载封面作为初始画面，并允许视频区域随播放器布局缩放。"""
        super().__init__(parent)
        reader = QImageReader(str(cover_path))
        reader.setAutoTransform(True)
        self.image = reader.read()
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumSize(0, 0)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def present(self, frame) -> None:
        """将有效视频帧转换为图像并请求重绘，无效帧保留当前画面。"""
        if frame.isValid():
            image = frame.toImage()
            if not image.isNull():
                self.image = image
                self.update()

    def paintEvent(self, event) -> None:
        """在圆角暗色区域内等比居中绘制当前画面，保留完整视频内容。"""
        painter = QPainter(self)
        painter.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        clip = QPainterPath()
        clip.addRoundedRect(QRectF(self.rect()), 10, 10)
        painter.setClipPath(clip)
        painter.fillRect(self.rect(), QColor("#090c11"))
        if not self.image.isNull():
            size = self.image.size().scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio)
            target = QRectF(
                (self.width() - size.width()) / 2, (self.height() - size.height()) / 2, size.width(), size.height()
            )
            painter.drawImage(target, self.image)
        painter.end()

    def mouseReleaseEvent(self, event) -> None:
        """在区域内释放鼠标左键时发出点击信号，供播放器切换播放状态。"""
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(event.position().toPoint()):
            self.clicked.emit()
        super().mouseReleaseEvent(event)


class ProjectStoryPlayer(QWidget):
    """管理页内短片的媒体资源、播放控制和宿主窗口全屏切换。"""

    close_requested = Signal()

    def __init__(self, story: ProjectStory, parent: QWidget) -> None:
        """创建归控件所有的媒体、音频和视频接收对象，并连接播放控件及局部快捷键。"""
        super().__init__(parent)
        self.setObjectName("projectStoryPlayer")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.story = story
        self._started = False
        self._released = False
        self._fullscreen = False
        self.changing_presentation = False
        self._home = None
        self._host = None
        self._placeholder = None
        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.audio.setVolume(0.7)
        self.player.setAudioOutput(self.audio)
        self.sink = QVideoSink(self)
        self.player.setVideoSink(self.sink)

        self.setStyleSheet(
            f"QWidget#projectStoryPlayer {{ background: {theme.color('bg.card')}; border-radius: 14px; }}"
            "QLabel { background: transparent; border: none; }"
            f"QToolButton {{ color: {theme.color('text.primary')}; background: transparent;"
            " border: none; border-radius: 6px; padding: 0; min-height: 0; }"
            f"QToolButton:hover, QToolButton:focus {{ background: {theme.color('bg.hover')}; }}"
            "QSlider { background: transparent; min-height: 18px; }"
            f"QSlider::groove:horizontal {{ height: 3px; background: {theme.color('stroke.control')};"
            " border: none; border-radius: 1px; }"
            f"QSlider::sub-page:horizontal {{ background: {theme.color('accent')}; border-radius: 1px; }}"
            f"QSlider::handle:horizontal {{ width: 10px; margin: -4px 0; background: {theme.color('accent')};"
            " border: none; border-radius: 5px; }"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 0, 12, 10)
        layout.setSpacing(0)
        header = QWidget(self)
        header.setFixedHeight(78)
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(10, 8, 10, 8)
        header_layout.setSpacing(18)
        information = QVBoxLayout()
        information.setSpacing(3)
        self.title_label = self._label(tr(story.title), 23, "text.primary")
        self.title_label.setStyleSheet(self.title_label.styleSheet() + "font-weight: 600;")
        information.addWidget(self.title_label)
        self.subtitle_label = self._label(tr(story.subtitle), 12, "text.secondary")
        information.addWidget(self.subtitle_label)
        header_layout.addLayout(information, 1)
        self.close_button = QPushButton(tr("收起短片"), header)
        self.close_button.setIcon(icons.get_icon("actions/chevron-up"))
        self.close_button.setAutoDefault(False)
        self.close_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.close_button.setFixedHeight(32)
        self.close_button.setToolTip(tr("收起短片（Esc）"))
        self.close_button.clicked.connect(self.close_requested)
        header_layout.addWidget(self.close_button)
        layout.addWidget(header)

        self.video = _VideoSurface(story.cover_path, self)
        self.video.clicked.connect(self._toggle_play)
        self.sink.videoFrameChanged.connect(self.video.present)
        layout.addWidget(self.video, 1)
        self.status_label = QLabel(tr("正在加载短片…"), self.video)
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.status_label.setWordWrap(True)
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.status_label.setStyleSheet("color: white; background: rgba(9, 12, 17, 190); border-radius: 8px;")
        self.video.installEventFilter(self)

        controls = QWidget(self)
        controls.setFixedHeight(68)
        control_layout = QVBoxLayout(controls)
        control_layout.setContentsMargins(6, 5, 6, 0)
        control_layout.setSpacing(0)
        self.seek_slider = QSlider(Qt.Orientation.Horizontal, controls)
        self.seek_slider.setAccessibleName(tr("播放进度"))
        self.seek_slider.setRange(0, 0)
        self.seek_slider.setEnabled(False)
        control_layout.addWidget(self.seek_slider)
        row = QHBoxLayout()
        row.setSpacing(8)
        self.play_button = self._button(tr("播放 / 暂停（空格）"), self._toggle_play)
        row.addWidget(self.play_button)
        self.time_label = self._label("00:00 / 00:00", 12, "text.secondary")
        self.time_label.setMinimumWidth(105)
        row.addWidget(self.time_label)
        row.addStretch(1)
        self.mute_button = self._button(tr("静音"), lambda: self.audio.setMuted(not self.audio.isMuted()))
        row.addWidget(self.mute_button)
        self.volume_slider = QSlider(Qt.Orientation.Horizontal, controls)
        self.volume_slider.setAccessibleName(tr("音量"))
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setValue(70)
        self.volume_slider.setFixedWidth(80)
        self.volume_slider.valueChanged.connect(lambda value: self.audio.setVolume(value / 100))
        row.addWidget(self.volume_slider)
        self.fullscreen_button = self._button(tr("全屏 / 退出全屏（F11）"), self._toggle_fullscreen)
        self.fullscreen_button.setIcon(icons.get_icon("actions/fullscreen"))
        row.addWidget(self.fullscreen_button)
        control_layout.addLayout(row)
        layout.addWidget(controls)

        self.player.positionChanged.connect(self._position_changed)
        self.player.durationChanged.connect(self._duration_changed)
        self.player.seekableChanged.connect(self.seek_slider.setEnabled)
        self.player.playbackStateChanged.connect(self._playback_changed)
        self.player.mediaStatusChanged.connect(self._media_status_changed)
        self.player.errorOccurred.connect(self._playback_error)
        self.audio.mutedChanged.connect(self._muted_changed)
        self.seek_slider.valueChanged.connect(self._seek_changed)
        self.seek_slider.sliderReleased.connect(lambda: self.player.setPosition(self.seek_slider.value()))
        self._shortcuts = []
        for key, callback in (("Space", self._toggle_play), ("F11", self._toggle_fullscreen)):
            shortcut = QShortcut(QKeySequence(key), self, activated=callback)
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            self._shortcuts.append(shortcut)
        self._playback_changed(self.player.playbackState())
        self._muted_changed(False)

    @staticmethod
    def _label(text: str, size: int, token: str) -> QLabel:
        """按主题色标和字号创建可换行的纯文本标签。"""
        label = QLabel(text)
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setWordWrap(True)
        label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        label.setStyleSheet(f"font-size: {size}px; color: {theme.color(token)};")
        return label

    def _button(self, tooltip: str, callback) -> QToolButton:
        """创建固定尺寸的播放工具按钮，绑定操作并设置提示和无障碍名称。"""
        button = QToolButton(self)
        button.setFixedSize(34, 32)
        button.setIconSize(QSize(18, 18))
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setToolTip(tooltip)
        button.setAccessibleName(tooltip)
        button.clicked.connect(callback)
        return button

    def _standard_icon(self, standard: QStyle.StandardPixmap) -> QIcon:
        """将平台标准媒体图标染为当前主题的主文本色。"""
        pixmap = self.style().standardIcon(standard).pixmap(20, 20)
        painter = QPainter(pixmap)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
        painter.fillRect(pixmap.rect(), QColor(theme.color("text.primary")))
        painter.end()
        return QIcon(pixmap)

    def start(self) -> None:
        """首次启动时检查短片文件并开始播放；文件缺失时显示错误，释放后不再启动。"""
        if self._started or self._released:
            return
        self._started = True
        self.setFocus(Qt.FocusReason.OtherFocusReason)
        if not self.story.video_path.is_file():
            self._show_error(tr("暂未找到项目短片，请稍后重试。"))
            return
        self.player.setSource(QUrl.fromLocalFile(str(self.story.video_path)))
        self.player.play()

    def resizeEvent(self, event) -> None:
        """在页内模式按视频宽高比及控制区高度调整播放器高度。"""
        super().resizeEvent(event)
        if not self._fullscreen:
            self.setFixedHeight(round(max(0, self.width() - 24) * 9 / 16) + 156)

    def _toggle_play(self) -> None:
        """切换播放与暂停，播放结束后从头重播；资源已释放或按钮禁用时忽略请求。"""
        if self._released or not self.play_button.isEnabled():
            return
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        else:
            if self.player.mediaStatus() == QMediaPlayer.MediaStatus.EndOfMedia:
                self.player.setPosition(0)
            self.player.play()

    def _playback_changed(self, state) -> None:
        """按实际播放状态同步播放按钮图标及无障碍操作名称。"""
        playing = state == QMediaPlayer.PlaybackState.PlayingState
        self.play_button.setAccessibleName(tr("暂停") if playing else tr("播放"))
        self.play_button.setIcon(icons.get_icon("actions/pause" if playing else "actions/play"))

    def _muted_changed(self, muted: bool) -> None:
        """按静音状态更新音量图标、按钮提示和无障碍名称。"""
        self.mute_button.setIcon(
            self._standard_icon(
                QStyle.StandardPixmap.SP_MediaVolumeMuted if muted else QStyle.StandardPixmap.SP_MediaVolume
            )
        )
        self.mute_button.setToolTip(tr("取消静音") if muted else tr("静音"))
        self.mute_button.setAccessibleName(self.mute_button.toolTip())

    def _duration_changed(self, duration: int) -> None:
        """更新进度条范围并刷新时间显示，阻止范围变化触发跳转播放。"""
        with QSignalBlocker(self.seek_slider):
            self.seek_slider.setRange(0, max(0, duration))
        self._position_changed(self.player.position())

    def _position_changed(self, position: int) -> None:
        """同步媒体位置与时间文案，用户拖动进度条期间保留其选择。"""
        if self.seek_slider.isSliderDown():
            return
        with QSignalBlocker(self.seek_slider):
            self.seek_slider.setValue(position)
        self.time_label.setText(f"{_clock(position)} / {_clock(self.player.duration())}")

    def _seek_changed(self, position: int) -> None:
        """实时显示选中的时间；非拖动操作立即跳转，拖动操作由释放事件提交。"""
        self.time_label.setText(f"{_clock(position)} / {_clock(self.player.duration())}")
        if not self.seek_slider.isSliderDown():
            self.player.setPosition(position)

    def _media_status_changed(self, status) -> None:
        """在媒体可播放或结束时隐藏加载提示，无效媒体转入错误显示。"""
        if status in (
            QMediaPlayer.MediaStatus.LoadedMedia,
            QMediaPlayer.MediaStatus.BufferedMedia,
            QMediaPlayer.MediaStatus.EndOfMedia,
        ):
            self.status_label.hide()
        elif status == QMediaPlayer.MediaStatus.InvalidMedia:
            self._playback_error()

    def _show_error(self, message: str) -> None:
        """显示播放错误并禁用播放和进度控制，避免继续操作无效媒体。"""
        self.status_label.setText(message)
        self.status_label.show()
        self.play_button.setEnabled(False)
        self.seek_slider.setEnabled(False)

    def _playback_error(self, *_args) -> None:
        """将媒体后端错误转换为统一的用户可见播放失败提示。"""
        self._show_error(tr("无法播放这段短片，请稍后重试。"))

    def _toggle_fullscreen(self) -> None:
        """借用宿主窗口全屏显示播放器，保存原父对象、布局位置和窗口状态以便恢复。"""
        if self._released:
            return
        if self._fullscreen:
            self._leave_fullscreen()
            return
        self.changing_presentation = True
        self._fullscreen = True
        self._home = self.parentWidget()
        self._host = self.window()
        self._host_state = self._host.windowState()
        self._host_geometry = self._host.geometry()
        self._placeholder = QWidget(self._home)
        self._placeholder.setFixedHeight(self.height())
        self._home.layout().replaceWidget(self, self._placeholder)
        # 更换父对象前将焦点交给稳定宿主，避免隐藏时导航列表自动切换页面。
        self._host.setFocus(Qt.FocusReason.OtherFocusReason)
        self.setParent(self._host)
        self.setMinimumHeight(0)
        self.setMaximumHeight(16777215)
        self._host.installEventFilter(self)
        self._host.showFullScreen()
        self.setGeometry(self._host.rect())
        self.show()
        self.raise_()
        self.setFocus(Qt.FocusReason.OtherFocusReason)
        self.fullscreen_button.setIcon(icons.get_icon("actions/fullscreen-exit"))
        self.changing_presentation = False

    def _leave_fullscreen(self) -> None:
        """把播放器放回原页面并删除占位控件，恢复宿主窗口状态、几何及焦点。"""
        if not self._fullscreen:
            return
        self.changing_presentation = True
        self._fullscreen = False
        self._host.removeEventFilter(self)
        self._host.setFocus(Qt.FocusReason.OtherFocusReason)
        self.setParent(self._home)
        self._home.layout().replaceWidget(self._placeholder, self)
        self._placeholder.deleteLater()
        self._placeholder = None
        self._host.setWindowState(self._host_state)
        if not self._host_state & (Qt.WindowState.WindowMaximized | Qt.WindowState.WindowFullScreen):
            self._host.setGeometry(self._host_geometry)
        self.show()
        self.setFocus(Qt.FocusReason.OtherFocusReason)
        self.fullscreen_button.setIcon(icons.get_icon("actions/fullscreen"))
        self._host = self._home = None
        self.changing_presentation = False

    def eventFilter(self, watched, event) -> bool:
        """随视频区域或全屏宿主尺寸变化调整状态提示及播放器几何。"""
        if watched is self.video and event.type() == QEvent.Type.Resize:
            self.status_label.setGeometry(self.video.rect().adjusted(24, 16, -24, -16))
        elif watched is self._host and self._fullscreen and event.type() == QEvent.Type.Resize:
            self.setGeometry(self._host.rect())
        return super().eventFilter(watched, event)

    def _escape(self) -> None:
        """优先退出全屏；处于页内模式时向页面发出收起短片请求。"""
        if self._fullscreen:
            self._leave_fullscreen()
        else:
            self.close_requested.emit()

    def event(self, event) -> bool:
        """让播放器优先接收 Esc，避免退出播放操作同时触发主窗口导航。"""
        # 播放器内优先接收退出键，再由自身按键处理，避免触发主窗口导航。
        if event.type() == QEvent.Type.ShortcutOverride and event.key() == Qt.Key.Key_Escape:
            event.accept()
            return True
        return super().event(event)

    def keyPressEvent(self, event) -> None:
        """消费 Esc 并执行退出操作，其余按键沿用控件默认处理。"""
        if event.key() == Qt.Key.Key_Escape:
            self._escape()
            event.accept()
            return
        super().keyPressEvent(event)

    def release_media(self) -> None:
        """幂等停止播放并清除媒体源、输出和当前图像，同时退出全屏以恢复页面结构。"""
        if self._released:
            return
        self._released = True
        self.player.stop()
        self.player.setSource(QUrl())
        self.player.setVideoOutput(None)
        self.player.setAudioOutput(None)
        self.video.image = QImage()
        self._leave_fullscreen()
