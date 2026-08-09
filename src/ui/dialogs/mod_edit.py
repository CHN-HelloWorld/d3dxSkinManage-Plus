"""提供 Mod 元数据与标签草稿编辑，支持事务保存、放弃确认及窄窗口布局。"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QEvent, QPoint, QSignalBlocker, QSize, Qt
from PySide6.QtGui import QAction, QTextOption
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTextEdit,
    QToolButton,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from src.models.mod import AgeRating, Mod
from src.models.tag import TagCommitResult, TagDraft
from src.ui import acrylic, icons, theme
from src.ui.widgets.tag_editor import plain_label
from src.ui.widgets.tag_picker import TagPicker
from src.utils.i18n import t as tr
from src.utils.logger import logger


class _FormScrollArea(QScrollArea):
    """沿嵌套滚动区域显示目标控件。"""

    def ensureWidgetVisible(self, child, xmargin=50, ymargin=50) -> None:
        """依次请求嵌套滚动区显示目标控件，再调整外层滚动位置，避免验证错误字段仍被内层遮挡。"""
        ancestor = child.parentWidget()
        while ancestor is not None and ancestor is not self:
            if isinstance(ancestor, QScrollArea):
                ancestor.ensureWidgetVisible(child, xmargin, ymargin)
            ancestor = ancestor.parentWidget()
        super().ensureWidgetVisible(child, xmargin, ymargin)


class _SideScrollArea(QScrollArea):
    """保持侧栏父对象稳定，窄屏时交给外层表单滚动。"""

    def __init__(self) -> None:
        """初始化侧栏滚动区，默认使用横向双栏布局的独立滚动模式。"""
        super().__init__()
        self._stacked = False

    def set_stacked(self, stacked: bool) -> None:
        """切换侧栏宽度约束和滚动策略，纵向堆叠时由外层表单承担滚动。"""
        self._stacked = stacked
        self.setMinimumWidth(0 if stacked else 258)
        self.setMaximumWidth(16777215 if stacked else 258)
        self.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff if stacked else Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        self.updateGeometry()

    def minimumSizeHint(self) -> QSize:
        """在堆叠模式返回容纳内容最小高度的尺寸，避免外层布局压缩侧栏字段。"""
        hint = super().minimumSizeHint()
        if self._stacked and self.widget() is not None:
            hint.setHeight(self.widget().minimumSizeHint().height() + self.frameWidth() * 2)
        return hint

    def sizeHint(self) -> QSize:
        """堆叠模式按内容和边框计算建议尺寸，其他情况沿用滚动区默认建议。"""
        if self._stacked and self.widget() is not None:
            return self.widget().sizeHint() + QSize(self.frameWidth() * 2, self.frameWidth() * 2)
        return super().sizeHint()

    def event(self, event) -> bool:
        """完成默认事件处理后，在堆叠模式的布局请求中更新几何建议以同步内容高度。"""
        result = super().event(event)
        if event.type() == QEvent.Type.LayoutRequest and getattr(self, "_stacked", False):
            # 详情变化会更新内容最小高度，窄屏模式由外层滚动区承载。
            self.updateGeometry()
        return result


class _WrappingPathEdit(QTextEdit):
    """元数据按可用宽度换行并滚动，不扩大窗口。"""

    def __init__(self, parent: QWidget | None = None, *, height: int = 88) -> None:
        """创建固定高度、任意位置换行的只读纯文本区，长路径不扩大窗口宽度。"""
        super().__init__(parent)
        self.setObjectName("mod-edit-readonly")
        self.setReadOnly(True)
        self.setAcceptRichText(False)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        self.setWordWrapMode(QTextOption.WrapMode.WrapAnywhere)
        self.setTabChangesFocus(True)
        self.document().setDocumentMargin(4)
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(height)

    def setText(self, text: str | None) -> None:
        """以纯文本显示路径或标识，空值转换为空字符串。"""
        self.setPlainText(text or "")

    def text(self) -> str:
        """返回未被视觉换行改变的完整纯文本内容。"""
        return self.toPlainText()


class _DiscardDialog(QDialog):
    """确认是否放弃未保存的元数据和标签草稿，接受表示放弃，拒绝表示继续编辑。"""

    def __init__(self, parent):
        """创建依附编辑窗口的确认框，默认操作为继续编辑。"""
        super().__init__(parent)
        self.setWindowTitle(tr("放弃本次修改？"))
        self.setWindowModality(Qt.WindowModality.WindowModal)
        layout = QVBoxLayout(self)
        layout.addWidget(plain_label(tr("尚未保存的 Mod 信息与标签修改将全部放弃。")))
        buttons = QHBoxLayout()
        self.continue_button = QPushButton(tr("继续编辑"))
        self.continue_button.setDefault(True)
        self.continue_button.clicked.connect(self.reject)
        self.discard_button = QPushButton(tr("放弃修改"))
        self.discard_button.setAutoDefault(False)
        self.discard_button.clicked.connect(self.accept)
        buttons.addWidget(self.continue_button)
        buttons.addWidget(self.discard_button)
        layout.addLayout(buttons)

    def showEvent(self, event) -> None:
        """显示确认框后将焦点交给继续编辑按钮，避免回车意外放弃草稿。"""
        super().showEvent(event)
        self.continue_button.setFocus()


class ModEditDialog(QDialog):
    """编辑 Mod 元数据和独立标签草稿，可由宿主注入事务保存回调，并在关闭前确认未保存修改。"""

    def __init__(
        self,
        mod: Mod,
        parent: QWidget | None = None,
    ) -> None:
        """根据 Mod 建立独立表单和标签草稿，记录初始字段供脏状态比较，保存逻辑可由宿主注入。"""
        super().__init__(parent)
        # 先完成原生表面设置，避免属性变化重新处理整张表单的样式。
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._mod = mod
        self._result: dict | None = None
        self._save_handler: Callable[[dict, TagDraft], TagCommitResult] | None = None
        self._saving = False
        self.discard_dialog = None
        self._wide_layout = None
        self._setup_ui()
        self._populate_data()
        self._original_fields = self._collect_fields()
        self._connect_change_signals()
        self._update_save_note()
        acrylic.apply_dialog_acrylic(self, tint_alpha=0.86)

    def _setup_ui(self) -> None:
        """构建可切换单双栏的表单、标签编辑和固定底栏，并设置键盘顺序及标签弹出边界。"""
        self.setWindowTitle(tr("编辑 Mod - {display_name}", display_name=self._mod.display_name))
        self.setMinimumSize(450, 340)
        self.resize(820, 536)
        self.setModal(True)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(1, 1, 1, 1)
        outer.setSpacing(0)

        heading = self._surface("mod-edit-heading")
        heading_layout = QHBoxLayout(heading)
        heading_layout.setContentsMargins(22, 16, 22, 14)
        heading_layout.setSpacing(12)
        edit_icon = plain_label()
        edit_icon.setObjectName("mod-edit-icon")
        edit_icon.setFixedSize(34, 34)
        edit_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icons.apply_pixmap(edit_icon, "actions/edit", 19, "accent")
        heading_layout.addWidget(edit_icon)
        heading_copy = QHBoxLayout()
        heading_copy.setSpacing(10)
        title = plain_label(tr("编辑 Mod"))
        title.setObjectName("mod-edit-title")
        divider = QFrame()
        divider.setObjectName("mod-edit-title-divider")
        divider.setFixedSize(1, 18)
        subtitle = plain_label(self._mod.display_name)
        self._heading_subtitle = subtitle
        subtitle.setObjectName("mod-edit-subtitle")
        subtitle.setToolTip(self._mod.display_name)
        subtitle.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        heading_copy.addWidget(title)
        heading_copy.addWidget(divider)
        heading_copy.addWidget(subtitle, 1)
        heading_layout.addLayout(heading_copy, 1)
        outer.addWidget(heading)

        self._form_scroll = _FormScrollArea()
        self._form_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._form_scroll.setWidgetResizable(True)
        self._form_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._form_scroll.viewport().setAutoFillBackground(False)
        self._form_content = self._surface("mod-edit-form")
        self._form_grid = QGridLayout(self._form_content)
        self._form_grid.setContentsMargins(22, 16, 22, 16)
        self._form_grid.setHorizontalSpacing(22)
        self._form_grid.setVerticalSpacing(16)
        self._basic_panel = self._build_basic_panel()
        self._side_panel = self._build_side_panel()
        self._side_scroll = _SideScrollArea()
        self._side_scroll.setProperty("modEditSurface", True)
        self._side_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._side_scroll.setWidgetResizable(True)
        self._side_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._side_scroll.viewport().setAutoFillBackground(False)
        self._side_scroll.setWidget(self._side_panel)
        self._form_scroll.setWidget(self._form_content)
        outer.addWidget(self._form_scroll, 1)

        self._save_error = plain_label()
        self._save_error.setObjectName("mod-edit-error")
        self._save_error.setWordWrap(True)
        self._save_error.setContentsMargins(22, 4, 22, 8)
        self._save_error.setMaximumHeight(54)
        self._save_error.hide()
        outer.addWidget(self._save_error)

        self._button_bar = self._surface("mod-edit-footer")
        buttons = QHBoxLayout(self._button_bar)
        buttons.setContentsMargins(20, 12, 20, 12)
        buttons.setSpacing(8)
        self._save_note = plain_label()
        self._save_note.setObjectName("mod-edit-save-note")
        self._save_note.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        buttons.addWidget(self._save_note, 1)
        self._cancel_button = QPushButton(tr("取消"))
        self._cancel_button.setAutoDefault(False)
        self._cancel_button.setMinimumWidth(76)
        self._cancel_button.clicked.connect(self.reject)
        buttons.addWidget(self._cancel_button)
        self._save_button = QPushButton(tr("保存修改"))
        self._save_button.setProperty("accent", True)
        self._save_button.setMinimumWidth(100)
        self._save_button.setDefault(True)
        self._save_button.setIconSize(QSize(15, 15))
        icons.apply_icon(self._save_button, "meta/check", "text.on_accent")
        self._save_button.setToolTip(tr("保存修改（Ctrl+Enter）"))
        self._save_button.clicked.connect(self._on_save)
        buttons.addWidget(self._save_button)
        outer.addWidget(self._button_bar)
        self.tag_picker.set_popup_boundary(self._button_bar)

        self._apply_style()
        self._update_columns()
        controls = [
            self._name_input,
            self._sync_folder_checkbox,
            self._author_input,
            self._version_input,
            self._url_input,
            self._desc_input,
            self._rating_combo,
            self.tag_picker.input,
        ]
        for first, second in zip(controls, controls[1:]):
            QWidget.setTabOrder(first, second)
        self.tag_picker.set_next_focus_widget(self._info_toggle)

    @staticmethod
    def _surface(name: str = "") -> QWidget:
        """创建允许收缩宽度的透明表单容器，以属性和对象名限定局部样式。"""
        widget = QWidget()
        widget.setProperty("modEditSurface", True)
        widget.setObjectName(name)
        widget.setMinimumWidth(0)
        return widget

    @staticmethod
    def _line_edit(accessible_name: str) -> QLineEdit:
        """创建固定高度且可横向收缩的单行输入框，并提供可访问名称。"""
        control = QLineEdit()
        control.setAccessibleName(accessible_name)
        control.setMinimumWidth(0)
        control.setFixedHeight(34)
        return control

    def _field(self, title: str, control: QWidget) -> QWidget:
        """将标题与输入控件组成字段容器，并把标题关联到控件以支持焦点导航。"""
        field = self._surface()
        layout = QVBoxLayout(field)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(5)
        label = plain_label(title)
        label.setProperty("modEditFieldLabel", True)
        label.setBuddy(control)
        layout.addWidget(label)
        layout.addWidget(control)
        return field

    def _build_basic_panel(self) -> QWidget:
        """构建名称、磁盘重命名选项及作者等基本字段，实际文件改名由保存处理器执行。"""
        panel = self._surface("mod-edit-basic")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        self._name_input = self._line_edit(tr("显示名称"))
        name_field = self._field(tr("显示名称"), self._name_input)
        self._sync_folder_checkbox = QCheckBox(tr("同时重命名 Mod 文件或文件夹"))
        self._sync_folder_checkbox.setObjectName("mod-edit-sync")
        self._sync_folder_checkbox.setChecked(True)
        sync_tip = tr(
            "勾选后：修改显示名时同时把磁盘上的 Mod 文件夹/压缩包改名；\n"
            "若同目录已存在同名条目，则自动追加 _1、_2 等后缀避免冲突。\n"
            "不勾选：仅修改软件内显示名，磁盘上的文件夹名保持不变。"
        )
        self._sync_folder_checkbox.setToolTip(sync_tip)
        sync_row = QHBoxLayout()
        sync_row.setSpacing(6)
        sync_row.addWidget(self._sync_folder_checkbox)
        help_icon = plain_label()
        help_icon.setToolTip(sync_tip)
        help_icon.setFixedSize(15, 15)
        icons.apply_pixmap(help_icon, "status/info", 13, "text.tertiary")
        sync_row.addWidget(help_icon)
        sync_row.addStretch()
        name_field.layout().addLayout(sync_row)
        layout.addWidget(name_field)

        self._author_input = self._line_edit(tr("作者"))
        self._version_input = self._line_edit(tr("版本"))
        self._author_input.setPlaceholderText(tr("未填写"))
        self._version_input.setPlaceholderText(tr("例如 1.0"))
        author_row = QHBoxLayout()
        author_row.setSpacing(12)
        author_row.addWidget(self._field(tr("作者"), self._author_input), 1)
        version_field = self._field(tr("版本"), self._version_input)
        version_field.setFixedWidth(106)
        author_row.addWidget(version_field)
        layout.addLayout(author_row)

        self._url_input = self._line_edit(tr("来源 URL"))
        self._url_input.setPlaceholderText("https://…")
        self._source_icon = QAction(self._url_input)
        self._source_icon.setToolTip(tr("来源链接"))
        icons.apply_icon(self._source_icon, "actions/link", "text.tertiary")
        self._url_input.addAction(self._source_icon, QLineEdit.ActionPosition.LeadingPosition)
        layout.addWidget(self._field(tr("来源 URL"), self._url_input))
        self._desc_input = QTextEdit()
        self._desc_input.setAccessibleName(tr("描述"))
        self._desc_input.setAcceptRichText(False)
        self._desc_input.setTabChangesFocus(True)
        self._desc_input.setPlaceholderText(tr("写下外观特点、使用说明或更新记录…"))
        self._desc_input.setFixedHeight(100)
        self._desc_input.setMinimumWidth(0)
        layout.addWidget(self._field(tr("描述"), self._desc_input))
        return panel

    def _build_side_panel(self) -> QWidget:
        """构建年龄分级、标签草稿与只读文件信息，并连接标签校验和保存提示。"""
        panel = self._surface("mod-edit-side")
        self._side_layout = QVBoxLayout(panel)
        self._side_layout.setContentsMargins(20, 0, 0, 0)
        self._side_layout.setSpacing(17)
        self._rating_combo = QComboBox()
        self._rating_combo.setAccessibleName(tr("年龄分级"))
        self._rating_combo.setFixedHeight(34)
        for rating in AgeRating:
            labels = {"G": tr("G · 全年龄"), "P": tr("P · 轻微敏感"), "R": tr("R · 限制级"), "X": tr("X · 18+ 内容")}
            self._rating_combo.addItem(labels[rating.value], rating.value)
        self._side_layout.addWidget(self._field(tr("年龄分级"), self._rating_combo))
        self.tag_picker = TagPicker(
            TagDraft.from_mod(self._mod),
            panel,
            selected_height_limit=90,
            inline_editor=True,
            show_hints=False,
        )
        self.tag_picker.save_requested.connect(self._on_save)
        self.tag_picker.changed.connect(self._update_save_note)
        self._tags_input = self.tag_picker.input
        self._side_layout.addWidget(self._field(tr("标签"), self.tag_picker))
        self._side_layout.addWidget(self._build_file_info())
        self._side_layout.addStretch(1)
        return panel

    def _build_file_info(self) -> QWidget:
        """创建默认折叠的路径、内容标识和导入时间区域，长文本可滚动且支持复制。"""
        section = self._surface("mod-edit-file-section")
        layout = QVBoxLayout(section)
        layout.setContentsMargins(0, 10, 0, 0)
        layout.setSpacing(6)
        toggle_row = QHBoxLayout()
        toggle_row.setSpacing(6)
        self._info_toggle = QPushButton(tr("文件信息"))
        self._info_toggle.setObjectName("mod-edit-file-toggle")
        self._info_toggle.setAccessibleName(tr("展开或收起文件信息"))
        self._info_toggle.setCheckable(True)
        self._info_toggle.setAutoDefault(False)
        self._info_toggle.setFlat(True)
        icons.apply_icon(self._info_toggle, "actions/folder", "text.secondary")
        self._info_toggle.setIconSize(QSize(13, 13))
        self._info_toggle.toggled.connect(self._toggle_file_info)
        toggle_row.addWidget(self._info_toggle, 1)
        readonly = plain_label(tr("只读"))
        readonly.setObjectName("mod-edit-readonly-badge")
        toggle_row.addWidget(readonly)
        self._info_chevron = QToolButton()
        self._info_chevron.setFixedSize(16, 16)
        self._info_chevron.setIconSize(QSize(13, 13))
        self._info_chevron.setAutoRaise(True)
        self._info_chevron.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._info_chevron.setAccessibleName(tr("展开或收起文件信息"))
        self._info_chevron.setStyleSheet("QToolButton { border: 0; background: transparent; padding: 0; }")
        self._info_chevron.clicked.connect(self._info_toggle.click)
        icons.apply_icon(self._info_chevron, "actions/chevron-down", "text.tertiary")
        toggle_row.addWidget(self._info_chevron)
        layout.addLayout(toggle_row)

        self._info_group = self._surface("mod-edit-file-info")
        info_layout = QVBoxLayout(self._info_group)
        info_layout.setContentsMargins(0, 0, 0, 0)
        info_layout.setSpacing(6)
        path_row = QHBoxLayout()
        path_row.addWidget(plain_label(tr("完整路径")), 1)
        self._copy_path_button = self._copy_button(tr("复制完整路径"), lambda: self._mod.path)
        path_row.addWidget(self._copy_path_button)
        info_layout.addLayout(path_row)
        self._path_label = _WrappingPathEdit()
        self._path_label.setAccessibleName(tr("完整文件路径"))
        info_layout.addWidget(self._path_label)
        hash_row = QHBoxLayout()
        hash_row.addWidget(plain_label("SHA-1"), 1)
        self._copy_id_button = self._copy_button(tr("复制 SHA-1"), lambda: self._mod.mod_id)
        hash_row.addWidget(self._copy_id_button)
        info_layout.addLayout(hash_row)
        self._id_label = _WrappingPathEdit(height=46)
        self._id_label.setAccessibleName("SHA-1")
        info_layout.addWidget(self._id_label)
        info_layout.addWidget(plain_label(tr("导入时间")))
        self._created_label = plain_label()
        self._created_label.setObjectName("mod-edit-created")
        self._created_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        info_layout.addWidget(self._created_label)
        self._info_group.hide()
        layout.addWidget(self._info_group)
        return section

    def _copy_button(self, title: str, value: Callable[[], str]) -> QToolButton:
        """创建复制按钮，点击时调用取值函数读取当前绑定 Mod 的最新信息。"""
        button = QToolButton()
        button.setAccessibleName(title)
        button.setToolTip(title)
        button.setFixedSize(25, 25)
        button.setIconSize(QSize(14, 14))
        icons.apply_icon(button, "actions/copy", "text.secondary")
        button.clicked.connect(lambda: self._copy_info(value(), button))
        return button

    @staticmethod
    def _copy_info(text: str, button: QWidget) -> None:
        """将完整文本写入系统剪贴板，并在触发按钮附近显示复制结果。"""
        QApplication.clipboard().setText(text)
        QToolTip.showText(button.mapToGlobal(QPoint(0, button.height())), tr("已复制"), button)

    def _toggle_file_info(self, expanded: bool) -> None:
        """同步只读信息区域的显隐及展开箭头方向，不修改表单数据。"""
        self._info_group.setVisible(expanded)
        icons.apply_icon(
            self._info_chevron, "actions/chevron-up" if expanded else "actions/chevron-down", "text.tertiary"
        )

    def _connect_change_signals(self) -> None:
        """将可编辑字段和磁盘同步选项的变化连接到未保存状态提示。"""
        for control in (self._name_input, self._author_input, self._version_input, self._url_input):
            control.textChanged.connect(self._update_save_note)
        self._desc_input.textChanged.connect(self._update_save_note)
        self._rating_combo.currentIndexChanged.connect(self._update_save_note)
        self._sync_folder_checkbox.toggled.connect(self._update_save_note)
