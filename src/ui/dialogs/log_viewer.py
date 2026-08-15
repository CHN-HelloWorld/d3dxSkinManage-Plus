"""只读查看应用日志，支持日期、级别筛选及复制。

大型日志仅读取末尾约一 MiB，避免大量文本影响界面响应。
"""

from __future__ import annotations

import re
from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices, QFont
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)

from src.ui import anim, icons, theme
from src.utils.i18n import t
from src.utils.logger import get_log_file
from src.utils.paths import get_log_dir

# 从带时间、级别及来源字段的日志行中提取级别。
_LEVEL_RE = re.compile(r"^\[[^\]]*\]\s*\[(\w+)\s*\]")
# 可选日志级别采用精确匹配，不包含其他级别。
_LEVEL_ORDER = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
_MAX_BYTES = 1024 * 1024  # 只尾读最后 1MB


class LogViewerDialog(QDialog):
    """只读日志查看器。由工具页「查看日志」按钮触发。"""

    def __init__(self, parent=None) -> None:
        """创建只读查看器，列出日志会话并立即加载当前选中文件的末尾内容。"""
        super().__init__(parent)
        anim.install_intro_fade(self)
        self.setWindowTitle(t("日志查看器"))
        self.resize(840, 560)
        self._raw_lines: list[str] = []
        self._build_ui()
        self._reload_file_list()
        self._refresh()

    # 界面构建。

    def _build_ui(self) -> None:
        """创建会话和级别筛选、只读文本区及复制与打开目录操作。"""
        root = QVBoxLayout(self)

        bar = QHBoxLayout()
        bar.addWidget(QLabel(t("启动会话:")))
        self._file_combo = QComboBox()
        self._file_combo.setMinimumWidth(150)
        self._file_combo.currentIndexChanged.connect(lambda _=0: self._refresh())
        bar.addWidget(self._file_combo)

        bar.addWidget(QLabel(t("级别:")))
        self._level_combo = QComboBox()
        self._level_combo.addItem(t("全部"), "")
        for lv in _LEVEL_ORDER:
            self._level_combo.addItem(lv, lv)
        self._level_combo.currentIndexChanged.connect(lambda _=0: self._apply_filter())
        bar.addWidget(self._level_combo)

        bar.addStretch()

        self._auto_scroll = QCheckBox(t("自动滚动到底部"))
        self._auto_scroll.setChecked(True)
        bar.addWidget(self._auto_scroll)

        refresh_btn = QPushButton(t("刷新"))
        icons.apply_icon(refresh_btn, "actions/refresh")
        refresh_btn.clicked.connect(self._refresh)
        bar.addWidget(refresh_btn)

        open_btn = QPushButton(t("打开日志目录"))
        icons.apply_icon(open_btn, "actions/open")
        open_btn.clicked.connect(self._open_folder)
        bar.addWidget(open_btn)

        copy_btn = QPushButton(t("复制"))
        copy_btn.clicked.connect(self._copy)
        bar.addWidget(copy_btn)

        root.addLayout(bar)

        self._view = QPlainTextEdit()
        self._view.setReadOnly(True)
        self._view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        font = QFont("Consolas")
        font.setStyleHint(QFont.StyleHint.Monospace)
        font.setPointSize(10)
        self._view.setFont(font)
        self._view.setStyleSheet(
            f"QPlainTextEdit {{ background: {theme.color('bg.subtle')}; "
            f"color: {theme.color('text.primary')}; "
            f"border: 1px solid {theme.color('stroke.card')}; "
            f"border-radius: {theme.radius('control')}px; padding: 6px; }}"
        )
        root.addWidget(self._view, 1)

        self._status = QLabel("")
        self._status.setStyleSheet(f"color: {theme.color('text.tertiary')};")
        root.addWidget(self._status)

    # 数据

    def _reload_file_list(self) -> None:
        """按日期和会话序号倒序加载日志列表，优先选中当前进程日志并暂时屏蔽选择信号。"""
        self._file_combo.blockSignals(True)
        self._file_combo.clear()
        # 会话日志位于 logs/YYYY-MM-DD/<序号>.log，按日期和同日序号倒序排列。
        files: list[Path] = []
        root = get_log_dir()
        if root.exists():
            for day_dir in root.iterdir():
                if day_dir.is_dir():
                    files.extend(day_dir.glob("*.log"))

        def _key(p: Path):
            """返回日志所属日期和数字序号作为排序键，非数字文件名按序号零处理。"""
            try:
                seq = int(p.stem)
            except ValueError:
                seq = 0
            return (p.parent.name, seq)

        files.sort(key=_key, reverse=True)
        current = get_log_file()
        current_resolved = Path(str(current)).resolve() if current is not None else None
        sel = 0
        for i, p in enumerate(files):
            self._file_combo.addItem(f"{p.parent.name} #{p.stem}", str(p))
            if current_resolved is not None and p.resolve() == current_resolved:
                sel = i
        if files:
            self._file_combo.setCurrentIndex(sel)
        self._file_combo.blockSignals(False)

    def _current_path(self) -> Path | None:
        """返回下拉框记录的日志路径，没有选中项时返回空值。"""
        data = self._file_combo.currentData()
        return Path(data) if data else None

    def _refresh(self) -> None:
        """读取所选日志尾部并重新筛选；截断首个残行，读取失败或文件缺失时清空缓存并提示。"""
        if self._file_combo.count() == 0:
            self._reload_file_list()
        path = self._current_path()
        if path is None or not path.exists():
            self._raw_lines = []
            self._view.setPlainText(t("暂无日志"))
            self._status.setText("")
            return
        try:
            size = path.stat().st_size
            with open(path, "rb") as f:
                if size > _MAX_BYTES:
                    f.seek(size - _MAX_BYTES)
                data = f.read()
            text = data.decode("utf-8", errors="replace")
            if size > _MAX_BYTES:
                text = text.split("\n", 1)[-1]  # 丢掉被尾读截断的首个半行
        except OSError as e:
            self._raw_lines = []
            self._view.setPlainText(f"{t('读取日志失败')}: {e}")
            self._status.setText("")
            return
        self._raw_lines = text.splitlines()
        self._apply_filter()

    def _apply_filter(self) -> None:
        """按级别精确筛选缓存日志，续行跟随上一条记录，并按设置滚动到底部。"""
        level = self._level_combo.currentData() or ""
        if not level:
            shown = self._raw_lines
        else:
            # 按单一级别精确筛选，而非按日志严重程度设置下限。
            shown = []
            keep = False
            for line in self._raw_lines:
                m = _LEVEL_RE.match(line)
                if m:
                    keep = m.group(1).upper() == level
                # 异常堆栈等无级别字段的续行沿用上一条记录的筛选结果。
                if keep:
                    shown.append(line)
        self._view.setPlainText("\n".join(shown))
        if self._auto_scroll.isChecked():
            sb = self._view.verticalScrollBar()
            sb.setValue(sb.maximum())
        self._status.setText(t("显示 {shown} / {total} 行", shown=len(shown), total=len(self._raw_lines)))

    # 动作

    def _copy(self) -> None:
        """将当前筛选后显示的全部文本写入系统剪贴板并更新状态提示。"""
        QApplication.clipboard().setText(self._view.toPlainText())
        self._status.setText(t("已复制到剪贴板"))

    def _open_folder(self) -> None:
        """请求桌面服务使用系统关联程序打开日志根目录。"""
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(get_log_dir())))
