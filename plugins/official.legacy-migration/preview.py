"""以只读树模型显示分类、Mod 和诊断，避免为大量资源逐行创建控件。"""

from __future__ import annotations

from dataclasses import dataclass, field

from PySide6.QtCore import QAbstractItemModel, QModelIndex, QObject, Qt
from PySide6.QtGui import QIcon

from .controller import MigrationController, MigrationItem
from .legacy import LegacyCategory, ScanIssue


def format_size(size: int) -> str:
    """使用固定二进制单位显示估算容量，不把目录大小当成目标磁盘占用保证。"""
    value = float(max(0, size))
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return "0 B"


@dataclass(eq=False)
class MigrationTreeNode:
    """轻量树节点只保存归属和行号，Mod 内容从当前控制器按需读取。"""

    parent: MigrationTreeNode | None = None
    row: int = 0
    category: LegacyCategory | None = None
    item_index: int = -1
    mod_count: int = 0
    children: list[MigrationTreeNode] = field(default_factory=list)
    issue_index: int = -1
    label: str = ""


class MigrationTreeModel(QAbstractItemModel):
    """在同一棵只读树中呈现分类和 Mod，按扫描身份重建结构。"""

    SearchRole = int(Qt.ItemDataRole.UserRole) + 1

    def __init__(
        self, controller: MigrationController, parent: QObject | None = None, *, folder_icon: QIcon | None = None
    ) -> None:
        """保留控制器及轻量行节点，不为每项创建控件或复制元数据。"""
        super().__init__(parent)
        self.controller = controller
        self.folder_icon = folder_icon
        self._scan = None
        self._root = MigrationTreeNode()
        self._categories: dict[tuple[str, ...], MigrationTreeNode] = {}
        self._mods: list[MigrationTreeNode] = []
        self._items: tuple[MigrationItem, ...] = ()
        self.total_bytes = 0
        self.category_count = 0
        self.object_count = 0
        self.excluded_count = 0
        self._category_statuses = {}
        self._issue_nodes: list[MigrationTreeNode] = []
        self._issues: tuple[ScanIssue, ...] = ()
        self._issue_group: MigrationTreeNode | None = None
        self._migration_issues = None
        self._issue_targets: dict[str, list[MigrationTreeNode]] = {}

    @property
    def issues(self) -> tuple[ScanIssue, ...]:
        """返回与当前树节点对应的诊断快照，供选中条目的详情读取。"""
        return self._issues

    def refresh(self) -> None:
        """模型先于界面更新，旧预览失效后不读取已清空的条目。"""
        if self._scan is self.controller.scan and self.controller.state in {"importing", "stopping"}:
            return
        if self._scan is not self.controller.scan:
            self.beginResetModel()
            self._root = MigrationTreeNode()
            self._categories = {}
            self._mods = []
            self._issue_nodes = []
            self._issues = ()
            self._issue_group = None
            self._issue_targets = {}
            for category in self.controller.categories:
                parent = self._categories.get(category.path[:-1], self._root)
                node = MigrationTreeNode(parent, len(parent.children), category)
                parent.children.append(node)
                self._categories[category.path] = node
                for name in {category.path[-1], " / ".join(category.path)}:
                    self._issue_targets.setdefault(name, []).append(node)
            for item_index, item in enumerate(self.controller.items):
                parent = self._categories[self.controller.destination_path(item.source)]
                node = MigrationTreeNode(parent, len(parent.children), item_index=item_index)
                parent.children.append(node)
                self._mods.append(node)
                for name in {item.source.name, item.source.sha, item.source.legacy_key, item.source.source_path} - {""}:
                    self._issue_targets.setdefault(name, []).append(node)
                ancestor = parent
                while ancestor is not None:
                    ancestor.mod_count += 1
                    ancestor = ancestor.parent
            self._scan = self.controller.scan
            self._sync_issues(notify=False)
            self._items = tuple(self.controller.items)
            self.total_bytes = sum(item.source.size_bytes for item in self._items)
            self.object_count = sum(category.node_type == "character" for category in self.controller.categories)
            self.category_count = len(self.controller.categories) - self.object_count
            self.excluded_count = (
                sum(issue.code in {"missing", "invalid"} for issue in self.controller.scan.issues)
                if self.controller.scan is not None
                else 0
            )
            self._category_statuses = {
                path: self._category_status(node.category) for path, node in self._categories.items()
            }
            self.endResetModel()
        else:
            self._sync_issues()
            for path, node in self._categories.items():
                status = self._category_status(node.category)
                if self._category_statuses.get(path) != status:
                    self._category_statuses[path] = status
                    index = self.createIndex(node.row, 3, node)
                    self.dataChanged.emit(index, index, [Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole])
            changed = [
                (index, previous, item)
                for index, (previous, item) in enumerate(zip(self._items, self.controller.items, strict=True))
                if previous is not item
            ]
            if not changed:
                return
            self._items = tuple(self.controller.items)
            roles = [Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole]
            for item_index, previous, item in changed:
                node = self._mods[item_index]
                self.total_bytes += item.source.size_bytes - previous.source.size_bytes
                first_column = 2 if previous.source is not item.source else 3
                self.dataChanged.emit(
                    self.createIndex(node.row, first_column, node), self.createIndex(node.row, 3, node), roles
                )
                if previous.source is not item.source:
                    index = self.createIndex(node.row, 0, node)
                    self.dataChanged.emit(index, index, [Qt.ItemDataRole.ToolTipRole])

    def _node_index(self, node: MigrationTreeNode) -> QModelIndex:
        """根节点没有索引，其余轻量节点按当前行号定位。"""
        return QModelIndex() if node is self._root else self.createIndex(node.row, 0, node)

    def issue_index(self, row: int) -> QModelIndex:
        """按问题列表的稳定序号定位树中的同一条诊断。"""
        return self._node_index(self._issue_nodes[row]) if 0 <= row < len(self._issue_nodes) else QModelIndex()

    def _append_node(
        self, parent: MigrationTreeNode, node: MigrationTreeNode, notify: bool, *, first: bool = False
    ) -> None:
        """新增展示节点时通知 Qt，不向控制器添加可导入内容。"""
        row = 0 if first else len(parent.children)
        if notify:
            self.beginInsertRows(self._node_index(parent), row, row)
        node.parent, node.row = parent, row
        parent.children.insert(row, node)
        if first:
            for position, child in enumerate(parent.children):
                child.row = position
        if notify:
            self.endInsertRows()

    def _remove_node(self, node: MigrationTreeNode) -> None:
        """清理旧诊断行，并同步剩余节点的当前行号。"""
        parent = node.parent
        self.beginRemoveRows(self._node_index(parent), node.row, node.row)
        parent.children.pop(node.row)
        for row, child in enumerate(parent.children):
            child.row = row
        self.endRemoveRows()
        if parent.item_index >= 0 or parent.category is not None:
            index = self.createIndex(parent.row, 3, parent)
            self.dataChanged.emit(index, index, [Qt.ItemDataRole.DisplayRole])

    def _sync_issues(self, notify: bool = True) -> None:
        """准确归属的诊断跟随原条目，其余放入只读分组，不猜测分类。"""
        count = len(self.controller.scan.issues) if self.controller.scan is not None else 0
        count += len(self.controller.migration_issues)
        if count == len(self._issues) and self._migration_issues is self.controller.migration_issues:
            return
        issues = self.controller.issues
        if issues[: len(self._issues)] != self._issues:
            for node in reversed(self._issue_nodes):
                self._remove_node(node)
            if self._issue_group is not None:
                self._remove_node(self._issue_group)
            self._issue_nodes = []
            self._issues = ()
            self._issue_group = None
        start = len(self._issue_nodes)
        self._issues = issues
        for row in range(start, len(issues)):
            issue = issues[row]
            targets = self._issue_targets.get(issue.name, ())
            parent = targets[0] if len(targets) == 1 and issue.code == "warning" else None
            if parent is None and issue.message == self.controller.ctx.i18n.t("目标分类创建失败。"):
                parent = self._categories.get(tuple(issue.name.split(" / ")))
            if parent is None:
                if self._issue_group is None:
                    self._issue_group = MigrationTreeNode(label=self.controller.ctx.i18n.t("未确定归属的条目"))
                    self._append_node(self._root, self._issue_group, notify, first=True)
                parent = self._issue_group
            node = MigrationTreeNode(issue_index=row)
            self._append_node(parent, node, notify)
            self._issue_nodes.append(node)
            if notify and parent is not self._issue_group:
                index = self.createIndex(parent.row, 3, parent)
                self.dataChanged.emit(index, index, [Qt.ItemDataRole.DisplayRole])
        self._issues = issues
        self._migration_issues = self.controller.migration_issues

    def index(self, row: int, column: int, parent: QModelIndex = QModelIndex()) -> QModelIndex:
        """以内部节点建立索引，非首列不拥有子树。"""
        if not self.hasIndex(row, column, parent):
            return QModelIndex()
        node = parent.internalPointer() if parent.isValid() else self._root
        return self.createIndex(row, column, node.children[row])

    def parent(self, index: QModelIndex) -> QModelIndex:
        """根分类没有父索引，其他行按保存的归属返回。"""
        if not index.isValid():
            return QModelIndex()
        parent = index.internalPointer().parent
        if parent is None or parent is self._root:
            return QModelIndex()
        return self.createIndex(parent.row, 0, parent)

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        """只在首列返回子节点，Qt 按需请求可见分支。"""
        if parent.isValid() and parent.column() != 0:
            return 0
        node = parent.internalPointer() if parent.isValid() else self._root
        return len(node.children)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        """名称、类型、数量或大小与状态分别展示。"""
        return 4

    def headerData(
        self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole
    ) -> str | None:
        """列标题说明树中的内容，而不是要求用户选择内容类型。"""
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole and 0 <= section < 4:
            tr = self.controller.ctx.i18n.t
            return (tr("分类与 Mod"), tr("类型"), tr("数量 / 大小"), tr("状态"))[section]
        return None

    def _category_status(self, category: LegacyCategory) -> str:
        """分类类型不冒充状态，按当前分类创建结果显示实际进度。"""
        tr = self.controller.ctx.i18n.t
        if any(category.path[:depth] in self.controller._category_errors for depth in range(1, len(category.path) + 1)):
            return tr("无法迁移")
        if category.path in self.controller._nodes:
            return tr("已就绪")
        if self.controller._active_operation == ("category", category):
            return tr("正在导入")
        if self.controller.state == "finished":
            return tr("未完成")
        return tr("待迁移")

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> str | QIcon | None:
        """实时读取 Mod 状态，分类显示汇总数量与文件夹图标。"""
        if not index.isValid():
            return None
        node = index.internalPointer()
        if role == self.SearchRole and index.column() == 0:
            if node.issue_index >= 0:
                issue = self._issues[node.issue_index]
                return "\n".join(
                    (issue.name, self.controller.ctx.i18n.t(issue.message, name=issue.name, source=issue.source))
                )
            if node.label:
                return node.label
            if node.category is not None:
                return " / ".join(node.category.path)
            if 0 <= node.item_index < len(self.controller.items):
                source = self.controller.items[node.item_index].source
                return source.name + "\n" + " / ".join(self.controller.destination_path(source))
        if node.issue_index >= 0:
            issue = self._issues[node.issue_index]
            tr = self.controller.ctx.i18n.t
            blocked = issue.code in {"missing", "invalid"}
            if role == Qt.ItemDataRole.DisplayRole:
                kind = (
                    tr("角色 / 对象")
                    if issue.message == "对象匹配多个旧版分类，已跳过相关 Mod"
                    else (tr("条目") if blocked else tr("提示"))
                )
                return (issue.name or tr("检查说明"), kind, "", tr("无法导入") if blocked else tr("需留意"))[
                    index.column()
                ]
            if role == Qt.ItemDataRole.ToolTipRole:
                return "\n".join(
                    value
                    for value in (issue.name, tr(issue.message, name=issue.name, source=issue.source), issue.source)
                    if value
                )
            return None
        if node.label:
            tr = self.controller.ctx.i18n.t
            if role == Qt.ItemDataRole.DisplayRole:
                return (node.label, tr("分组"), "", "")[index.column()]
            if role == Qt.ItemDataRole.ToolTipRole:
                return tr("无法确定归属的条目仅在此展示，不会自动分配分类或加入迁移。")
            return None
        if node.category is not None:
            category = node.category
            tr = self.controller.ctx.i18n.t
            if role == Qt.ItemDataRole.DecorationRole and index.column() == 0:
                return self.folder_icon
            if role == Qt.ItemDataRole.ToolTipRole:
                return " / ".join(category.path)
            if role == Qt.ItemDataRole.DisplayRole:
                return (
                    category.path[-1],
                    tr("角色 / 对象") if category.node_type == "character" else tr("分类"),
                    tr("{count} 个 Mod", count=node.mod_count),
                    self._category_status(category)
                    + (
                        " · " + tr("需留意")
                        if node.children
                        and any(
                            child.issue_index >= 0 and self._issues[child.issue_index].code == "warning"
                            for child in node.children
                        )
                        else ""
                    ),
                )[index.column()]
        elif 0 <= node.item_index < len(self.controller.items):
            return self._mod_data(node, index.column(), role)
        return None

    def _mod_data(self, node: MigrationTreeNode, column: int, role: int) -> str | None:
        """按树的原生列格式化当前 Mod，状态和来源选择均读取控制器快照。"""
        if role not in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole):
            return None
        item = self.controller.items[node.item_index]
        source = item.source
        tr = self.controller.ctx.i18n.t
        statuses = {
            "pending": tr("待迁移"),
            "running": tr("正在导入"),
            "succeeded": tr("已导入"),
            "skipped": tr("已存在，不重复导入"),
            "failed": tr("未完成，可重试"),
            "cancelled": tr("未完成，已停止"),
        }
        status = statuses[item.status]
        if item.error or item.warnings:
            status += " · " + tr("有提示，请查看报告")
        if source.cache_path and source.source_path == source.cache_path:
            status += " · " + tr("使用解压后的文件")
        elif source.cache_changed:
            status += " · " + tr("可保留旧版修改")
        if role == Qt.ItemDataRole.ToolTipRole:
            return "\n".join(
                value
                for value in (
                    source.name,
                    " / ".join(self.controller.destination_path(source)),
                    format_size(source.size_bytes),
                    status,
                    item.error,
                    *item.warnings,
                    source.source_path,
                )
                if value
            )
        if node.children:
            status += " · " + tr("需留意")
        return (source.name, tr("Mod"), format_size(source.size_bytes), status)[column]

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        """查看和搜索均不改变迁移范围，树中不出现勾选框。"""
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
