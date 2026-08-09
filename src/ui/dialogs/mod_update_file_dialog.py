"""从在线 Mod 的多个下载文件中选择更新目标。"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QStyle,
    QStyleOptionButton,
    QStylePainter,
    QVBoxLayout,
    QWidget,
)

from src.services.gamebanana.models import GbModFile
from src.services.gamebanana.update_checker import UpdateInfo, pick_default_file
from src.ui import acrylic, anim, icons, theme
from src.utils.i18n import t as tr


def _human_size(n: int) -> str:
    """按每级 1024 将文件字节数格式化为无小数的紧凑容量文本。"""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f}{unit}"
        n /= 1024
    return f"{n:.0f}TB"


class _FileRadioButton(QRadioButton):
    """适应窄窗口，同时保留完整文件名的可访问文本。"""

    def __init__(self, text: str) -> None:
        """保留完整按钮文本并允许横向收缩，固定最小行高以容纳文件选择操作。"""
        super().__init__(text)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.setMinimumHeight(34)

    def paintEvent(self, event) -> None:
        """按单选按钮可用内容宽度绘制中部省略文本，不改变完整文件名及可访问文本。"""
        option = QStyleOptionButton()
        self.initStyleOption(option)
        width = self.style().subElementRect(QStyle.SubElement.SE_RadioButtonContents, option, self).width()
        option.text = self.fontMetrics().elidedText(self.text(), Qt.TextElideMode.ElideMiddle, max(0, width))
        QStylePainter(self).drawControl(QStyle.ControlElement.CE_RadioButton, option)


class ModUpdateFileDialog(QDialog):
    """为单个 Mod 收集待安装文件的选择，下载和安装由接受结果的宿主执行。"""

    def __init__(self, files: list[GbModFile], *, local_original_name: str = "", parent=None) -> None:
        """复制候选文件列表并根据本地原始名称选择默认项，显示版本和容量供用户确认。"""
        super().__init__(parent)
        anim.install_intro_fade(self)
        self.setWindowTitle(tr("选择要更新的文件"))
        self.resize(420, 420)
        self._files = list(files)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(tr("这个 Mod 有多个文件，选择要安装的版本：")))
        self._list = QListWidget()
        default = pick_default_file(self._files, local_original_name) if self._files else None
        default_row = 0
        for i, f in enumerate(self._files):
            ver = f" · {f.version}" if f.version else ""
            item = QListWidgetItem(f"{f.file_name}{ver}  （{_human_size(f.size_bytes)}）")
            item.setData(Qt.ItemDataRole.UserRole, f.file_id)
            self._list.addItem(item)
            if default is not None and f.file_id == default.file_id:
                default_row = i
        self._list.setCurrentRow(default_row)
        layout.addWidget(self._list, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        acrylic.apply_dialog_acrylic(self)

    def selected_file(self) -> GbModFile | None:
        """按当前行保存的文件标识返回原候选对象，没有有效选中项时返回空值。"""
        item = self._list.currentItem()
        if item is None:
            return None
        fid = item.data(Qt.ItemDataRole.UserRole)
        for f in self._files:
            if f.file_id == fid:
                return f
        return None


class ModUpdateBatchFileDialog(QDialog):
    """启动批次前收集全部变体文件选择。"""

    def __init__(
        self, infos: list[UpdateInfo], *, local_original_names: dict[str, str] | None = None, parent=None
    ) -> None:
        """为每个 Mod 预选匹配文件，仅为多候选项创建选择卡片，并在选择齐全后允许启动。"""
        super().__init__(parent)
        self.setObjectName("modUpdateFilePicker")
        self.setWindowTitle(tr("选择更新文件"))
        self.resize(580, 530)
        self.setMinimumSize(380, 300)
        anim.install_intro_fade(self)
        self._files: dict[str, list[GbModFile]] = {}
        self._groups: dict[str, QButtonGroup] = {}
        self._selected: dict[str, GbModFile] = {}
        names = local_original_names or {}
        outer = QVBoxLayout(self)
        outer.setContentsMargins(22, 20, 22, 18)
        outer.setSpacing(12)
        heading = QHBoxLayout()
        icon = QLabel()
        icon.setPixmap(icons.pixmap("mod-updates/box", 24, "accent"))
        heading.addWidget(icon)
        title = QLabel(tr("先确认要安装的文件"))
        title.setObjectName("pickerTitle")
        heading.addWidget(title, 1)
        outer.addLayout(heading)
        multi = sum(len(info.candidate_files) > 1 for info in infos)
        hint = QLabel(
            tr(
                "本次更新 {count} 个 Mod，其中 {multi} 个提供多个文件。每个 Mod 选择一个。",
                count=len(infos),
                multi=multi,
            )
        )
        hint.setWordWrap(True)
        hint.setObjectName("pickerHint")
        outer.addWidget(hint)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        choices = QVBoxLayout(content)
        choices.setContentsMargins(0, 0, 5, 0)
        choices.setSpacing(12)
        for info in infos:
            mid = info.local_mod_id
            files = list(info.candidate_files or [info.latest_file])
            self._files[mid] = files
            default = pick_default_file(files, names.get(mid, ""))
            if default is not None:
                self._selected[mid] = default
            if len(files) < 2:
                continue
            card = QFrame()
            card.setObjectName("pickerGroup")
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(12, 12, 12, 12)
            name = QLabel(info.local_name or mid)
            name.setTextFormat(Qt.TextFormat.PlainText)
            name.setWordWrap(True)
            name.setObjectName("pickerModName")
            card_layout.addWidget(name)
            group = QButtonGroup(self)
            self._groups[mid] = group
            for index, file in enumerate(files):
                version = f" · {file.version}" if file.version else ""
                radio = _FileRadioButton(f"{file.file_name}{version}  ·  {_human_size(file.size_bytes)}")
                radio.setToolTip(radio.text())
                radio.setAccessibleName(radio.text())
                # 作者提供的长文件名不能使对话框超出屏幕。
                radio.setMinimumWidth(0)
                group.addButton(radio, index)
                card_layout.addWidget(radio)
                radio.setChecked(default is not None and file.file_id == default.file_id)
            group.idClicked.connect(lambda index, mid=mid: self._choose(mid, index))
            choices.addWidget(card)
        choices.addStretch()
        scroll.setWidget(content)
        outer.addWidget(scroll, 1)
        footer = QHBoxLayout()
        footer.setSpacing(10)
        self._summary = QLabel()
        self._summary.setObjectName("pickerHint")
        footer.addWidget(self._summary, 1)
        cancel = QPushButton(tr("取消"))
        cancel.setAutoDefault(False)
        cancel.clicked.connect(self.reject)
        footer.addWidget(cancel)
        self._start = QPushButton(tr("开始更新"))
        self._start.setObjectName("pickerStart")
        self._start.setIconSize(QSize(14, 14))
        icons.apply_icon(self._start, "mod-updates/download", "text.on_accent")
        self._start.clicked.connect(self.accept)
        self._start.setDefault(True)
        footer.addWidget(self._start)
        outer.addLayout(footer)
        self._update_summary()
        self.setStyleSheet(
            f"#modUpdateFilePicker {{ background:{theme.color('bg.card')}; }}"
            "#pickerTitle { font-size:17px; font-weight:600; }"
            "#pickerModName { font-weight:600; }"
            f"#pickerHint {{ color:{theme.color('text.secondary')}; font-size:12px; }}"
            f"#pickerGroup {{ border:1px solid {theme.color('stroke.card')}; border-radius:8px; }}"
            "QRadioButton { padding:7px 2px; }"
            "QPushButton { min-height:30px; padding:0 14px; }"
            f"#pickerStart {{ background:{theme.color('accent')}; color:{theme.color('text.on_accent')};"
            " border:none; border-radius:5px; }"
        )
        acrylic.apply_dialog_acrylic(self)

    def _choose(self, mid: str, index: int) -> None:
        """按按钮组提供的有效 Mod 标识和候选索引更新选择，再刷新容量汇总。"""
        self._selected[mid] = self._files[mid][index]
        self._update_summary()

    def _update_summary(self) -> None:
        """汇总已选文件数量和非负容量，仅在每个 Mod 都有选择时启用开始按钮。"""
        size = sum(max(0, file.size_bytes) for file in self._selected.values())
        self._summary.setText(tr("已选 {count} 个文件 · {value}", count=len(self._selected), value=_human_size(size)))
        self._start.setEnabled(len(self._selected) == len(self._files))

    def selected_files(self) -> dict[str, GbModFile]:
        """返回 Mod 标识到所选文件对象的独立映射副本，供宿主创建更新批次。"""
        return dict(self._selected)
