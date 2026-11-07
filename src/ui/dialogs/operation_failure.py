"""展示受影响对象和删除后果，关闭提示不会执行删除。"""

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

from src.ui.error_messages import user_error_message
from src.utils.i18n import t as tr


class OperationFailureDialog(QDialog):
    """说明失败对象与删除后果，通过接受或拒绝结果将删除决定交还调用方。"""

    def __init__(self, error, parent=None, *, target_name="", delete_method="permanent"):
        """按错误携带的类别或 Mod 标识构建提示；默认聚焦关闭，只有显式删除才返回接受。"""
        super().__init__(parent)
        self.setWindowTitle(tr("操作失败"))
        layout = QVBoxLayout(self)
        label = QLabel(user_error_message(error), self)
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setWordWrap(True)
        layout.addWidget(label)
        is_category = bool(getattr(error, "node_id", None))
        kind = tr("类别") if is_category else "Mod"
        name = target_name or Path(getattr(error, "path", "")).name or tr("当前项")
        target = QLabel(f"{kind}：{name}", self)
        target.setTextFormat(Qt.TextFormat.PlainText)
        target.setWordWrap(True)
        layout.addWidget(target)
        if is_category:
            action = tr("永久删除类别")
            consequence = tr("将永久删除该类别及其中的 Mod 文件和子类别，无法恢复。")
        elif delete_method == "recycle_bin":
            action = tr("移入回收站")
            consequence = tr("将从 Mod 库删除此 Mod，清理其部署文件，并将源文件或文件夹移入回收站。")
        else:
            action = tr("永久删除 Mod")
            consequence = tr("将永久删除此 Mod 的源文件或文件夹及部署文件，无法恢复。")
        warning = QLabel(consequence, self)
        warning.setWordWrap(True)
        layout.addWidget(warning)
        buttons = QHBoxLayout()
        buttons.addStretch()
        close = QPushButton(tr("关闭"), self)
        close.setDefault(True)
        close.clicked.connect(self.reject)
        buttons.addWidget(close)
        delete = QPushButton(action, self)
        delete.setAutoDefault(False)
        delete.setProperty("btnColor", "danger")
        delete.clicked.connect(self.accept)
        buttons.addWidget(delete)
        layout.addLayout(buttons)
        close.setFocus()
        self.resize(360, 240)
