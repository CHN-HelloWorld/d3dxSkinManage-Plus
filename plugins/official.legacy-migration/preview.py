"""以只读表格模型显示迁移预览，避免为大量资源逐行创建控件。"""

from __future__ import annotations

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt

from .controller import MigrationController


def format_size(size: int) -> str:
    """使用固定二进制单位显示估算容量，不把目录大小当成目标磁盘占用保证。"""
    value = float(max(0, size))
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return "0 B"


class MigrationPreviewModel(QAbstractTableModel):
    """按当前筛选延迟读取单元格内容，保留完整路径作为工具提示。"""

    def __init__(self, controller: MigrationController, parent=None) -> None:
        """绑定当前会话，不复制 Mod 元数据或为每行分配 Qt 对象。"""
        super().__init__(parent)
        self.controller = controller
        self.mode = "mods"
        self._scan = None
        self._count = 0
        self._issues = ()

    def set_mode(self, mode: str) -> None:
        """切换可导入资源、分类或扫描问题视图。"""
        if mode not in {"mods", "categories", "issues"} or mode == self.mode:
            return
        self.beginResetModel()
        self.mode = mode
        self._count = self._row_count()
        self.endResetModel()

    def refresh(self) -> None:
        """扫描变更时重建行索引，复制结果变化时仅刷新显示数据。"""
        controller = self.controller
        issue_count = len(controller.scan.issues) if controller.scan is not None else 0
        issue_count += len(controller.migration_issues)
        if self._scan is not controller.scan or len(self._issues) != issue_count:
            self._issues = controller.issues
        count = self._row_count()
        if self._scan is not controller.scan or count != self._count:
            self.beginResetModel()
            self._scan, self._count = controller.scan, count
            self.endResetModel()
        elif count:
            self.dataChanged.emit(self.index(0, 0), self.index(count - 1, 3))

    def _row_count(self) -> int:
        """根据筛选计算行数，异常列表包括扫描和复制阶段的诊断。"""
        if self.mode == "issues":
            return len(self._issues)
        if self.mode == "categories":
            return len(self.controller.categories)
        return len(self.controller.items)

    def rowCount(self, parent=QModelIndex()) -> int:
        """表格没有嵌套子行。"""
        return 0 if parent.isValid() else self._count

    def columnCount(self, parent=QModelIndex()) -> int:
        """所有视图均使用四列，切换时保持稳定布局。"""
        return 0 if parent.isValid() else 4

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        """按视图翻译列标题，行号由 Qt 默认处理。"""
        if orientation != Qt.Orientation.Horizontal or role != Qt.ItemDataRole.DisplayRole or not 0 <= section < 4:
            return None
        tr = self.controller.ctx.i18n.t
        if self.mode == "issues":
            headers = (tr("类型"), tr("条目"), tr("来源 / 详情"), tr("说明"))
        elif self.mode == "categories":
            headers = (tr("名称"), tr("上级分类"), tr("类型"), tr("预览图"))
        else:
            headers = (tr("名称"), tr("目标分类"), tr("估算容量"), tr("结果 / 说明"))
        return headers[section]

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        """在可见单元格请求时格式化内容，完整诊断仍可从工具提示读取。"""
        if not index.isValid() or not 0 <= index.row() < self._count:
            return None
        if role not in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole):
            return None
        tr = self.controller.ctx.i18n.t
        if self.mode == "issues":
            issue = self._issues[index.row()]
            codes = {"missing": tr("文件缺失"), "invalid": tr("无法迁移"), "warning": tr("提示")}
            values = (
                codes.get(issue.code, tr("提示")),
                issue.name,
                issue.source,
                tr(issue.message, name=issue.name, source=issue.source),
            )
            return (
                "\n".join(value for value in values if value)
                if role == Qt.ItemDataRole.ToolTipRole
                else values[index.column()]
            )
        if self.mode == "categories":
            category = self.controller.categories[index.row()]
            values = (
                category.path[-1],
                " / ".join(category.path[:-1]),
                tr("对象") if category.node_type == "character" else tr("分类"),
                tr("包含预览图") if category.thumbnail_path else tr("无"),
            )
            if role == Qt.ItemDataRole.ToolTipRole:
                return "\n".join(value for value in (" / ".join(category.path), category.thumbnail_path) if value)
            return values[index.column()]
        item = self.controller.items[index.row()]
        statuses = {
            "pending": tr("等待复制"),
            "running": tr("正在复制"),
            "succeeded": tr("成功"),
            "skipped": tr("已存在，跳过"),
            "failed": tr("失败"),
            "cancelled": tr("已取消"),
        }
        details = " · ".join(value for value in (statuses[item.status], item.error, *item.warnings) if value)
        values = (
            item.source.name,
            " / ".join(self.controller.destination_path(item.source)),
            format_size(item.source.size_bytes),
            details,
        )
        if role == Qt.ItemDataRole.ToolTipRole:
            return "\n".join((*values, item.source.source_path))
        return values[index.column()]

    def flags(self, index):
        """迁移预览只允许选择，不接受编辑、拖放或勾选修改。"""
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
