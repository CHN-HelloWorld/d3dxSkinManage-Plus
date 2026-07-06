"""独立配置图片、视频和场景背景来源。"""

import json
from contextlib import ExitStack
from pathlib import Path

from PySide6.QtCore import QSignalBlocker, QSize, Qt
from PySide6.QtGui import QImageReader, QPainter, QPixmap
from PySide6.QtWidgets import (
    QBoxLayout,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QSlider,
    QVBoxLayout,
)

from src.ui import icons, theme
from src.ui.backgrounds.engine import EnginePathValidator
from src.ui.settings_style import color, stroke, surface
from src.ui.widgets.settings_components import ChoiceSegments, icon_label, plain_widget, style_button, text_label
from src.utils import fs
from src.utils.i18n import t as tr


class _AssetPathLabel(QLabel):
    """在有限宽度内省略路径中部，并通过提示保留完整素材路径。"""

    def set_path(self, path):
        """保存完整路径作为提示，并按当前标签宽度更新省略显示。"""
        self._path = path
        self.setToolTip(path)
        self._elide()

    def _elide(self):
        """按当前字体和宽度省略路径中部，尚未设置路径时显示空文本。"""
        self.setText(
            self.fontMetrics().elidedText(
                getattr(self, "_path", ""), Qt.TextElideMode.ElideMiddle, max(1, self.width())
            )
        )

    def resizeEvent(self, event):
        """在标签宽度变化后重新计算路径省略位置。"""
        super().resizeEvent(event)
        self._elide()


class _AssetThumbnail(QLabel):
    """占位图随主题变化，不替换实际预览图。"""

    def __init__(self):
        """建立居中的素材缩略图区域，初始使用紧凑占位尺寸。"""
        super().__init__()
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.clear()

    def clear(self):
        """清除素材位图并恢复占位图标所需的尺寸。"""
        super().clear()
        self.setFixedSize(32, 40)

    def setPixmap(self, pixmap):
        """切换到素材预览尺寸后显示给定缩略图。"""
        self.setFixedSize(88, 60)
        super().setPixmap(pixmap)

    def paintEvent(self, event):
        """有预览图时沿用标签绘制，否则使用当前主题的图片图标占位。"""
        if not self.pixmap().isNull():
            super().paintEvent(event)
            return
        painter = QPainter(self)
        painter.drawPixmap(0, (self.height() - 32) // 2, icons.pixmap("settings/image", 32, "settings.muted"))


class _AssetCard(QFrame):
    """窄设置面板将文件操作排列到详情下方。"""

    def resizeEvent(self, event):
        """根据卡片宽度在横向和纵向排列之间切换，避免窄面板挤压素材操作。"""
        super().resizeEvent(event)
        self.layout().setDirection(
            QBoxLayout.Direction.TopToBottom if self.width() < 480 else QBoxLayout.Direction.LeftToRight
        )


class BackgroundSettingsMixin:
    """为设置页提供背景来源、素材和动态播放参数控件；宿主负责配置存储、布局辅助及变更信号。"""

    def _build_background_image_status(self, rows):
        """创建默认隐藏的图片加载错误行，并将重试按钮连接到背景播放请求。"""
        self._bg_image_status = text_label(role="error", wrap=True)
        self._bg_image_retry_button = style_button(QPushButton(tr("重试")), icon="refresh")
        self._bg_image_retry_button.clicked.connect(lambda: self.bg_playback_requested.emit("retry"))
        controls = QHBoxLayout()
        controls.addWidget(self._bg_image_status, 1)
        controls.addWidget(self._bg_image_retry_button)
        self._bg_image_error_row = self._add_row(rows, tr("图片加载失败"), controls)
        self._bg_image_error_row.hide()

    def _build_background_sources(self, rows):
        """建立关闭、图片、视频和场景来源选择及关闭提示，保留统一的下拉框数据接口。"""
        self._bg_source_rows = rows
        self._bg_source_combo = QComboBox(self)
        self._bg_source_combo.hide()  # 保持来源选择接口一致。
        for label, value in (
            (tr("关闭"), "off"),
            (tr("图片"), "image"),
            (tr("视频"), "video"),
            (tr("WE 场景"), "scene"),
        ):
            self._bg_source_combo.addItem(label, value)
        self._bg_source_segments = ChoiceSegments(self._bg_source_combo)
        self._bg_source_buttons = {
            self._bg_source_combo.itemData(index): button
            for index, button in enumerate(self._bg_source_segments.buttons)
        }
        self._bg_source_group = self._bg_source_segments._group
        self._add_row(rows, tr("背景来源"), self._bg_source_segments, tr("关闭背景时保留各类型的素材和显示设置。"))
        self._bg_source_combo.currentIndexChanged.connect(self._on_bg_source_changed)
        self._bg_off_note, note = self._bg_plain_panel()
        note.setContentsMargins(20, 0, 20, 18)
        line = QHBoxLayout()
        line.setSpacing(7)
        line.addWidget(icon_label("info", 14, "settings.muted"))
        line.addWidget(text_label(tr("背景已关闭，恢复主题底色。"), "muted", wrap=True), 1)
        note.addLayout(line)
        rows.addWidget(self._bg_off_note)

    @staticmethod
    def _bg_plain_panel():
        """返回无外边距的透明容器及其纵向布局，供背景设置各分区复用。"""
        panel = plain_widget()
        rows = QVBoxLayout(panel)
        rows.setContentsMargins(0, 0, 0, 0)
        rows.setSpacing(0)
        return panel, rows

    def _build_bg_asset(self, source, title, file_filter):
        """创建带预览、路径和选择移除操作的素材卡片，将路径变化绑定到对应背景配置项。"""
        panel, outer = self._bg_plain_panel()
        outer.setContentsMargins(20, 14, 20, 18)
        card = _AssetCard()
        card.setObjectName("backgroundAsset")
        self._restyle(
            card,
            lambda: (
                f"#backgroundAsset {{ background-color: {surface('soft', over='panel')};"
                f"border: 1px dashed {stroke('control')}; border-radius: 7px; }}"
                "#backgroundAsset QLabel { background: transparent; border: none; }"
            ),
        )
        row = QBoxLayout(QBoxLayout.Direction.LeftToRight, card)
        row.setContentsMargins(17, 17, 17, 17)
        row.setSpacing(14)
        details = plain_widget()
        details.setMinimumWidth(0)
        detail_row = QHBoxLayout(details)
        detail_row.setContentsMargins(0, 0, 0, 0)
        detail_row.setSpacing(14)
        thumb = _AssetThumbnail()
        detail_row.addWidget(thumb)
        info = QVBoxLayout()
        info.setSpacing(3)
        display_title = tr("WE 场景") if source == "scene" else title
        name = text_label(tr("尚未选择{display_title}", display_title=display_title), "label", wrap=True)
        name.setMinimumWidth(0)
        path_label = _AssetPathLabel()
        path_label.setTextFormat(Qt.TextFormat.PlainText)
        path_label.setMinimumWidth(0)
        path_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self._restyle(
            path_label,
            lambda: f"font-size: {theme.font('size_caption')}px; color: {color('muted')}; background: transparent;",
        )
        info.addWidget(name)
        info.addWidget(path_label)
        detail_row.addLayout(info, 1)
        row.addWidget(details, 1)
        edit = QLineEdit(self)
        edit.hide()
        choose = style_button(QPushButton(tr("选择文件")), icon="folder")
        choose.setAccessibleName(tr("选择{display_title}文件", display_title=display_title))

        def browse():
            """以当前路径打开限定类型的文件选择器，仅在确认文件后更新素材路径。"""
            path, _ = QFileDialog.getOpenFileName(self, tr("选择{title}", title=title), edit.text(), file_filter)
            if path:
                edit.setText(path)

        choose.clicked.connect(browse)
        clear = style_button(QPushButton(tr("移除")), icon="close")
        clear.setAccessibleName(tr("移除{display_title}", display_title=display_title))
        clear.setVisible(bool(edit.text()))
        clear.clicked.connect(edit.clear)
        edit.textChanged.connect(lambda value: clear.setVisible(bool(value)))
        actions = QHBoxLayout()
        actions.setSpacing(8)
        actions.addWidget(choose)
        actions.addWidget(clear)
        actions.addStretch(1)
        row.addLayout(actions)
        outer.addWidget(card)
        if not hasattr(self, "_bg_assets"):
            self._bg_assets = {}
        self._bg_assets[source] = (edit, name, path_label, thumb, display_title)
        if not hasattr(self, "_bg_asset_clear_buttons"):
            self._bg_asset_clear_buttons = {}
        self._bg_asset_clear_buttons[source] = clear
        key = "path" if source == "image" else f"{source}_path"
        edit.textChanged.connect(lambda value: self._on_bg_path_changed(key, value))
        return panel, edit

    def _refresh_bg_asset(self, source):
        """更新素材名称和缩略图；场景预览必须位于项目目录内，无效素材保留占位显示。"""
        edit, name, label, thumb, title = self._bg_assets[source]
        path = edit.text()
        label.set_path(path or (tr("选择 Wallpaper Engine 的场景项目") if source == "scene" else tr("从本地选择素材")))
        self._bg_asset_clear_buttons[source].setVisible(bool(path))
        if thumb.property("assetPath") == path:
            return
        name.setText(Path(path).name if path else tr("尚未选择{title}", title=title))
        thumb.setProperty("assetPath", path)
        thumb.clear()
        preview = Path(path) if path and source == "image" else None
        if source == "scene" and path:
            try:
                project = Path(path)
                data = json.loads(project.read_text(encoding="utf-8-sig"))
                name.setText(data.get("title") or project.parent.name)
                candidate = (project.parent / data.get("preview", "preview.jpg")).resolve()
                if candidate.is_relative_to(project.parent.resolve()):
                    preview = candidate
            except (OSError, ValueError, TypeError, AttributeError):
                pass
        if preview is not None:
            reader = QImageReader(fs.native_path(str(preview)))
            reader.setAutoTransform(True)
            if reader.size().isValid():
                reader.setScaledSize(
                    reader.size().scaled(QSize(176, 120), Qt.AspectRatioMode.KeepAspectRatioByExpanding)
                )
            image = reader.read()
            if not image.isNull():
                pixmap = QPixmap.fromImage(image).scaled(
                    88, 60, Qt.AspectRatioMode.KeepAspectRatioByExpanding, Qt.TransformationMode.SmoothTransformation
                )
                thumb.setPixmap(pixmap.copy((pixmap.width() - 88) // 2, (pixmap.height() - 60) // 2, 88, 60))

    def _build_dynamic_background_panels(self, layout):
        """创建引擎校验、视频场景素材及播放参数区域，连接异步校验和背景控制信号。"""
        self._bg_settings_layout = layout
        self._bg_we_panel, self._bg_we_layout = self._bg_plain_panel()
        self._bg_source_rows.addWidget(self._bg_we_panel)
        self._bg_engine_validator = EnginePathValidator(self)
        self._bg_engine_validator.status_changed.connect(self._on_bg_engine_status)
        self._bg_engine_panel, rows = self._bg_plain_panel()
        self._bg_engine_status = text_label(tr("请填写或自动查找 Wallpaper Engine 程序路径。"), "smallHelp", wrap=True)
        fields, controls = self._bg_plain_panel()
        fields.setMaximumWidth(280)
        controls.setSpacing(8)
        self._bg_engine_path_edit = QLineEdit()
        self._bg_engine_path_edit.setMinimumWidth(0)
        self._bg_engine_path_edit.setAccessibleName(tr("Wallpaper Engine 程序路径"))
        self._bg_engine_path_edit.setPlaceholderText(tr("选择 Wallpaper Engine 程序…"))
        self._bg_engine_path_edit.textChanged.connect(lambda value: self._on_bg_path_changed("engine_path", value))
        controls.addWidget(self._bg_engine_path_edit)
        actions = QHBoxLayout()
        actions.setSpacing(8)
        discover = style_button(QPushButton(tr("自动查找")), icon="search")
        discover.clicked.connect(self._bg_engine_validator.discover)
        actions.addWidget(discover)
        browse = style_button(QPushButton(tr("浏览")), icon="folder")

        def choose_engine():
            """选择场景引擎程序，并通过路径输入框触发统一的配置与校验流程。"""
            path, _ = QFileDialog.getOpenFileName(
                self,
                tr("Wallpaper Engine 程序"),
                self._bg_engine_path_edit.text(),
                tr("Wallpaper Engine (wallpaper64.exe wallpaper32.exe);;程序 (*.exe)"),
            )
            if path:
                self._bg_engine_path_edit.setText(path)

        browse.clicked.connect(choose_engine)
        actions.addWidget(browse)
        clear = style_button(QPushButton(tr("清除")), link=True)
        clear.clicked.connect(self._bg_engine_path_edit.clear)
        actions.addWidget(clear)
        actions.addStretch(1)
        controls.addLayout(actions)
        self._add_row(
            rows, tr("Wallpaper Engine 程序路径"), fields, tr("选择安装目录中的 wallpaper64.exe；填写后自动检测。")
        )
        self._bg_engine_validator.path_found.connect(self._bg_engine_path_edit.setText)
        self._restyle(
            self._bg_engine_status,
            lambda: (
                f"color: {color('danger' if self._bg_engine_validator.status == 'invalid' else 'secondary')};"
                " background: transparent; padding: 0 20px 16px;"
            ),
        )
        rows.addWidget(self._bg_engine_status)
        self._bg_we_layout.addWidget(self._bg_engine_panel)

        self._bg_video_panel, self._bg_video_path_edit = self._build_bg_asset(
            "video", tr("视频"), tr("视频 (*.mp4 *.webm *.mkv *.avi *.mov);;所有文件 (*)")
        )
        self._bg_source_rows.addWidget(self._bg_video_panel)
        self._bg_scene_panel, self._bg_scene_path_edit = self._build_bg_asset(
            "scene", tr("场景"), tr("WE 场景 (project.json)")
        )
        self._bg_we_layout.addWidget(self._bg_scene_panel)
        self._bg_motion_panel, rows = self._make_group(tr("动态背景"), icon="image")
        motion_rows = rows
        self._bg_fps_combo = QComboBox()
        self._bg_fps_combo.setMinimumWidth(210)
        for fps in (30, 60):
            self._bg_fps_combo.addItem(f"{fps} FPS" + (tr("（推荐）") if fps == 30 else ""), fps)
        self._add_row(rows, tr("背景帧率"), self._bg_fps_combo, tr("30 FPS 为默认值；60 FPS 更流畅。"))
        self._bg_fps_combo.currentIndexChanged.connect(
            lambda: self._save_bg_dynamic_parameter("fps", self._bg_fps_combo.currentData())
        )
        self._bg_scale_combo = QComboBox()
        self._bg_scale_combo.setMinimumWidth(210)
        self._bg_scale_combo.addItem(tr("2 倍（推荐）"), 2)
        self._bg_scale_combo.addItem(tr("1 倍（较低开销）"), 1)
        self._bg_quality_row = self._add_row(
            rows, tr("场景质量"), self._bg_scale_combo, tr("2 倍渲染后缩小，改善边缘锯齿。")
        )
        self._bg_scale_combo.currentIndexChanged.connect(
            lambda: self._save_bg_dynamic_parameter("scene_scale", self._bg_scale_combo.currentData())
        )
        self._bg_volume_slider = QSlider(Qt.Orientation.Horizontal)
        self._bg_volume_slider.setRange(0, 100)
        self._bg_volume_label = text_label(tr("静音"), "muted")
        controls = QHBoxLayout()
        controls.setSpacing(8)
        controls.addWidget(self._bg_volume_slider)
        controls.addWidget(self._bg_volume_label)
        self._bg_volume_slider.setFixedWidth(205)
        self._bg_volume_label.setFixedWidth(40)
        self._bg_volume_row = self._add_row(rows, tr("背景音量"), controls, tr("默认静音。"))
        self._bg_volume_slider.valueChanged.connect(self._on_bg_volume_changed)
        self._bg_minimize_help = text_label(tr("始终开启"))
        self._restyle(self._bg_minimize_help, lambda: f"color: {color('teal')}; background: transparent;")
        self._add_row(rows, tr("最小化时暂停"), self._bg_minimize_help, tr("恢复窗口后继续播放；不会覆盖手动暂停。"))
        layout.addWidget(self._bg_motion_panel)
        self._bg_playback_panel, rows = self._bg_plain_panel()
        self._bg_playback_status = text_label(wrap=True)
        self._restyle(
            self._bg_playback_status,
            lambda: (
                f"color: {color('danger' if getattr(self, '_bg_playback_state', '') == 'error' else 'secondary')};"
                " background: transparent; padding: 0 20px 16px;"
            ),
        )
        controls = QHBoxLayout()
        controls.setSpacing(8)
        self._bg_pause_button = style_button(QPushButton(tr("暂停播放")))
        self._bg_pause_button.clicked.connect(
            lambda: self.bg_playback_requested.emit("resume" if self._bg_playback_state == "paused" else "pause"),
        )
        self._bg_retry_button = style_button(QPushButton(tr("重试")), icon="refresh")
        self._bg_retry_button.clicked.connect(lambda: self.bg_playback_requested.emit("retry"))
        controls.addWidget(self._bg_pause_button)
        controls.addWidget(self._bg_retry_button)
        playback_row = self._add_row(rows, tr("播放控制"), controls, tr("暂停后保留当前画面。"))
        playback_row.setProperty("settingsSeparator", True)
        rows.addWidget(self._bg_playback_status)
        motion_rows.addWidget(self._bg_playback_panel)
        self.set_background_status("off", tr("请选择素材。"))

    def _load_dynamic_background_settings(self, bg):
        """阻断控件信号后回填背景配置，再校验引擎路径并刷新分区及素材预览。"""
        widgets = (
            self._bg_source_combo,
            self._bg_engine_path_edit,
            self._bg_video_path_edit,
            self._bg_scene_path_edit,
            self._bg_fps_combo,
            self._bg_scale_combo,
            self._bg_volume_slider,
        )
        with ExitStack() as blockers:
            for widget in widgets:
                blockers.enter_context(QSignalBlocker(widget))
            source = bg.get("source", "image") if bg.get("enabled") else "off"
            self._bg_source_combo.setCurrentIndex(self._bg_source_combo.findData(source))
            for key in ("engine", "video", "scene"):
                getattr(self, f"_bg_{key}_path_edit").setText(bg.get(f"{key}_path", "") or "")
            self._bg_fps_combo.setCurrentIndex(self._bg_fps_combo.findData(bg.get("fps", 30)))
            self._bg_scale_combo.setCurrentIndex(self._bg_scale_combo.findData(bg.get("scene_scale", 2)))
            volume = bg.get("volume", 0)
            self._bg_volume_slider.setValue(volume)
            self._bg_volume_label.setText(f"{volume}%" if volume else tr("静音"))
        path = self._bg_engine_path_edit.text()
        if path != self._bg_engine_validator.path:
            self._bg_engine_validator.set_path(path)
        self._update_background_panels()
        for source in self._bg_assets:
            self._refresh_bg_asset(source)
