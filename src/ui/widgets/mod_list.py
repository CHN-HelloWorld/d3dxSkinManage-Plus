"""Mod 列表的部署反馈：状态胶囊与整行底部的 2px 进度线。"""

from __future__ import annotations

import math
from time import monotonic

from PySide6.QtCore import QPoint, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QFontMetrics, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QGraphicsEffect,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTableWidget,
    QTableWidgetItem,
)

from src.ui import anim, icons, theme
from src.ui.widgets.tag_display import TagNameCell
from src.utils.i18n import t as tr


class _ModStatusDelegate(QStyledItemDelegate):
    """先保留原生选中底色，再绘制独立的状态胶囊。"""

    def paint(self, painter, option, index) -> None:
        """保留表格原生选择背景，按部署、失败和启用状态绘制独立图标及状态胶囊。"""
        table = self.parent()
        type_item = table.item(index.row(), 0)
        if not type_item or type_item.data(Qt.ItemDataRole.UserRole + 1) != "mod":
            super().paint(painter, option, index)
            return

        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        # opt.icon 是借用引用；先复制，否则清空原生图标时也会清掉胶囊图标。
        text, item_icon = opt.text, QIcon(opt.icon)
        opt.text = ""
        opt.icon = QIcon()
        opt.backgroundBrush = QBrush()
        table.style().drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, table)

        mod_id = type_item.data(Qt.ItemDataRole.UserRole)
        busy = table.is_deploying(mod_id)
        failed = bool(table.deploy_error(mod_id))
        enabled = bool(type_item.data(table.ROLE_ENABLED))
        token = "accent" if busy else "danger" if failed else "success" if enabled else "text.tertiary"
        foreground = QColor(theme.color(token))
        font = opt.font
        font.setPixelSize(int(theme.font("size_small")))
        font.setBold(enabled or failed)
        metrics = QFontMetrics(font)
        available = option.rect.adjusted(6, 3, -6, -3)
        width = min(available.width(), metrics.horizontalAdvance(text) + 36)
        height = min(24, available.height())
        if width <= 0 or height <= 0:
            return
        pill = QRectF(available.left(), available.center().y() - height / 2, width, height)
        icon_rect = QRectF(pill.left() + 7, pill.center().y() - 7, 14, 14)
        text_rect = pill.adjusted(27, 0, -7, 0)

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setClipRect(option.rect)
        if busy or failed or enabled:
            background = QColor(foreground)
            background.setAlpha(30)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(background)
            painter.drawRoundedRect(pill, theme.radius("small"), theme.radius("small"))
        if busy:
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(foreground, 1.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            angle = int((monotonic() % 1) * 360) if anim.is_enabled() else 45
            painter.drawArc(icon_rect, -angle * 16, 280 * 16)
        else:
            item_icon.paint(painter, icon_rect.toRect())
        painter.setFont(font)
        painter.setPen(foreground)
        painter.drawText(
            text_rect,
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            metrics.elidedText(text, Qt.TextElideMode.ElideRight, max(0, int(text_rect.width()))),
        )
        painter.restore()


class _RowRevealEffect(QGraphicsEffect):
    """每个绘制步骤显示一行，不改变滚动位置。"""

    def __init__(self, table, rows):
        """保存待揭示的可见行，并建立按绘制完成情况推进的逐行显示定时器。"""
        super().__init__(table.viewport())
        self._table = table
        self._rows = rows
        self._count = self._painted = 0
        self._snapshot = QPixmap()
        self._offset = None
        self._timer = QTimer(self)
        self._timer.setInterval(26)
        self._timer.timeout.connect(self._advance)
        self._timer.start()

    def _advance(self):
        """前一步已绘制后才增加可见行数，动画禁用、表格隐藏或全部完成时移除效果。"""
        if not anim.is_enabled() or not self._table.isVisible():
            self._table.cancel_row_reveal()
            return
        if self._painted < self._count:
            return
        if self._count == len(self._rows):
            self._table.cancel_row_reveal()
            return
        self._count += 1
        self.update()

    def draw(self, painter):
        """复用视口快照并裁剪到当前最后一行，记录本轮实际绘制的进度。"""
        if not self._count:
            return
        if self._snapshot.isNull():
            self._offset = QPoint()
            self._snapshot = self.sourcePixmap(
                Qt.CoordinateSystem.LogicalCoordinates, self._offset, QGraphicsEffect.PixmapPadMode.NoPad
            )
        if self._snapshot.isNull():
            return
        last = self._rows[self._count - 1]
        bottom = self._table.rowViewportPosition(last) + self._table.rowHeight(last)
        painter.save()
        painter.setClipRect(QRect(0, 0, self._table.viewport().width(), bottom), Qt.ClipOperation.IntersectClip)
        painter.drawPixmap(self._offset, self._snapshot)
        painter.restore()
        self._painted = self._count


class ModListTable(QTableWidget):
    """反馈按 Mod ID 保存；筛选、重建和视图切换不会把进度移到另一行。

    部署结果仍由外层提交。本控件只展示真实进度，不提前改变启用态。
    共用定时器仅在表格可见时刷新活动行，避免为各个单元格创建动画控件。
    """

    tag_clicked = Signal(str)

    ROLE_ENABLED = Qt.ItemDataRole.UserRole + 3
    STATUS_COLUMN = 4
    FINISH_SECONDS = 1.2

    def __init__(self, parent=None):
        """建立按 Mod 标识保存的部署反馈、延迟行数据和可见名称控件缓存。"""
        super().__init__(0, 5, parent)
        self._mod_rows: dict[str, int] = {}
        self._progress: dict[str, float | None] = {}
        self._errors: dict[str, str] = {}
        self._completed: dict[str, float] = {}
        self._row_factory = None
        self._pending_rows: set[int] = set()
        self._name_data: dict[int, tuple] = {}
        self._name_cells: dict[int, TagNameCell] = {}
        self._syncing_rows = False
        self._row_reveal_effect = None
        self._feedback_timer = QTimer(self)
        self._feedback_timer.setInterval(33)
        self._feedback_timer.timeout.connect(self._advance_feedback)
        self.setItemDelegateForColumn(self.STATUS_COLUMN, _ModStatusDelegate(self))

    def setRowCount(self, rows: int) -> None:
        """重置行数前取消揭示动画并清理行索引、延迟填充状态及名称控件。"""
        self.cancel_row_reveal()
        self._mod_rows.clear()
        self._row_factory = None
        self._pending_rows.clear()
        for row in tuple(self._name_cells):
            self.removeCellWidget(row, 1)
        self._name_cells.clear()
        self._name_data.clear()
        super().setRowCount(rows)

    def set_row_factory(self, factory) -> None:
        """只立即创建身份单元格，其余详情按显示或读取需要填充。

        选择和键盘导航可直接访问所有行，不为隐藏行批量创建控件。
        """
        self._row_factory = factory
        self._pending_rows = set(range(self.rowCount()))
        self._sync_visible_rows()

    def item(self, row: int, column: int):
        """访问非标识列时按需填充尚未构造的行，再返回表格项。"""
        if column and row in self._pending_rows:
            self._populate_row(row)
        return super().item(row, column)

    def _populate_row(self, row: int) -> None:
        """在移除待填充标记后调用行工厂，避免工厂读取同一行导致重入。"""
        if row not in self._pending_rows or self._row_factory is None:
            return
        self._pending_rows.remove(row)
        self._row_factory(row)

    def set_name_data(self, row: int, name: str, tags, *, updatable: bool) -> None:
        """保存名称、标签和更新状态，使已有名称控件失效并安排视口刷新。"""
        self._name_data[row] = (name, tuple(tags), updatable)
        if row in self._name_cells:
            self.removeCellWidget(row, 1)
            self._name_cells.pop(row)
        # 单次元数据更新后可能没有立即重绘。
        self.viewport().update()

    def _sync_visible_rows(self) -> None:
        """只构造视口附近的行和名称控件，移出视口后仍保留具有焦点或打开标签浮层的控件。"""
        if self._syncing_rows or not self.isVisible() or not self.rowCount():
            return
        self._syncing_rows = True
        try:
            first = self.rowAt(0)
            if first < 0:
                return
            last = self.rowAt(max(0, self.viewport().height() - 1))
            if last < 0:
                last = self.rowCount() - 1
            visible = {
                row for row in range(max(0, first - 1), min(self.rowCount(), last + 2)) if not self.isRowHidden(row)
            }
            for row, cell in tuple(self._name_cells.items()):
                popup = cell._tags._popup
                focus = QApplication.focusWidget()
                if (
                    row not in visible
                    and not (popup is not None and popup.isVisible())
                    and not (focus is not None and (focus is cell or cell.isAncestorOf(focus)))
                ):
                    self.removeCellWidget(row, 1)
                    self._name_cells.pop(row)
            for row in sorted(visible):
                self._populate_row(row)
                if row in self._name_cells or row not in self._name_data:
                    continue
                name, tags, updatable = self._name_data[row]
                cell = TagNameCell(name, tags, updatable=updatable)
                cell.tag_clicked.connect(self.tag_clicked)
                self.setCellWidget(row, 1, cell)
                self._name_cells[row] = cell
        finally:
            self._syncing_rows = False

    def scrollContentsBy(self, dx: int, dy: int) -> None:
        """滚动前取消逐行揭示效果，滚动后同步新进入视口的行内容。"""
        self.cancel_row_reveal()
        super().scrollContentsBy(dx, dy)
        self._sync_visible_rows()

    def reveal_visible_rows(self) -> None:
        """在动画启用且表格可见时，为当前未隐藏的可见行创建逐行揭示效果。"""
        self.cancel_row_reveal()
        if not anim.is_enabled() or not self.isVisible() or not self.rowCount():
            return
        first = self.rowAt(0)
        if first < 0:
            return
        last = self.rowAt(max(0, self.viewport().height() - 1))
        if last < 0:
            last = self.rowCount() - 1
        rows = [row for row in range(first, last + 1) if not self.isRowHidden(row)]
        if rows:
            self._row_reveal_effect = _RowRevealEffect(self, rows)
            self.viewport().setGraphicsEffect(self._row_reveal_effect)

    def cancel_row_reveal(self) -> None:
        """停止逐行显示定时器，并仅移除由本表格安装的揭示效果。"""
        effect = getattr(self, "_row_reveal_effect", None)
        if effect is not None:
            self._row_reveal_effect = None
            effect._timer.stop()
            if self.viewport().graphicsEffect() is effect:
                self.viewport().setGraphicsEffect(None)

    def mousePressEvent(self, event) -> None:
        """鼠标操作前结束揭示动画，再按普通表格行为处理选择。"""
        self.cancel_row_reveal()
        super().mousePressEvent(event)

    def keyPressEvent(self, event) -> None:
        """键盘操作前结束揭示动画，确保用户可以立即访问完整表格。"""
        self.cancel_row_reveal()
        super().keyPressEvent(event)

    def is_deploying(self, mod_id: str) -> bool:
        """判断指定 Mod 是否仍有待完成的部署进度记录。"""
        return mod_id in self._progress

    def deploy_error(self, mod_id: str) -> str:
        """返回指定 Mod 最近记录的部署错误，未记录时返回空字符串。"""
        return self._errors.get(mod_id, "")

    def status_text(self, mod_id: str, enabled: bool) -> str:
        """优先表达部署进度和错误，再依据真实启用状态返回显示文案。"""
        if mod_id in self._progress:
            progress = self._progress[mod_id]
            if progress is None:
                return tr("启用中…")
            if progress >= 1:
                return tr("正在完成…")
            return tr("启用中 {value}%", value=int(progress * 100))
        if mod_id in self._errors:
            return tr("启用失败")
        return tr("已启用") if enabled else tr("已禁用")

    def refresh_mod_status(self, row: int) -> None:
        """校验行类型后同步 Mod 索引、状态文字、图标及无障碍提示，避免重复写入相同文案。"""
        type_item = self.item(row, 0)
        if not type_item or type_item.data(Qt.ItemDataRole.UserRole + 1) != "mod":
            return
        mod_id = type_item.data(Qt.ItemDataRole.UserRole)
        self._mod_rows[mod_id] = row
        enabled = bool(type_item.data(self.ROLE_ENABLED))
        if enabled:
            self._errors.pop(mod_id, None)
        busy = self.is_deploying(mod_id)
        error = self.deploy_error(mod_id)
        text = self.status_text(mod_id, enabled)
        item = self.item(row, self.STATUS_COLUMN)
        if item is None:
            item = QTableWidgetItem()
            self.setItem(row, self.STATUS_COLUMN, item)
        # 进度信号可能比整数百分比更密；避免每帧改模型和触发无关选区信号。
        if item.text() != text:
            item.setText(text)
        token = "accent" if busy else "danger" if error else "success" if enabled else "text.tertiary"
        item.setForeground(QColor(theme.color(token)))
        item.setBackground(QBrush())
        icon_name = "status/error" if error else "status/enabled" if enabled else "status/disabled"
        icon_key = f"{icon_name}:{theme.color(token)}"
        if item.data(Qt.ItemDataRole.UserRole + 5) != icon_key:
            item.setIcon(icons.get_icon(icon_name, token))
            item.setData(Qt.ItemDataRole.UserRole + 5, icon_key)
        tooltip = error or (tr("正在启用，可继续浏览其他 Mod；处理完成前请勿重复操作。") if busy else text)
        item.setToolTip(tooltip)
        item.setData(Qt.ItemDataRole.AccessibleTextRole, text)

    def set_deploy_progress(self, mod_id: str, progress: float | None) -> None:
        """记录限定范围内的进度，未知或非有限进度按等待状态显示，并清除旧完成及错误反馈。"""
        self._errors.pop(mod_id, None)
        self._completed.pop(mod_id, None)
        self._progress[mod_id] = (
            max(0.0, min(1.0, progress)) if progress is not None and math.isfinite(progress) else None
        )
        self._refresh_feedback_row(mod_id)
        self._sync_feedback_timer()

    def finish_deploy(self, mod_id: str, *, success: bool, error: str = "") -> None:
        """结束进度记录，显示限时成功反馈或保留错误提示；真实启用状态仍由外层更新。"""
        self._progress.pop(mod_id, None)
        if success:
            self._errors.pop(mod_id, None)
            if anim.is_enabled():
                self._completed[mod_id] = monotonic() + self.FINISH_SECONDS
        else:
            self._completed.pop(mod_id, None)
            self._errors[mod_id] = error or tr("无法完成 Mod 部署，请检查压缩包和部署目录后重试。")
        self._refresh_feedback_row(mod_id)
        self._sync_feedback_timer()

    def _row_for_mod(self, mod_id: str) -> int | None:
        """校验缓存行仍绑定目标 Mod，失效时丢弃索引并返回 None。"""
        row = self._mod_rows.get(mod_id)
        if row is None:
            return None
        item = self.item(row, 0)
        if item is None or item.data(Qt.ItemDataRole.UserRole) != mod_id:
            self._mod_rows.pop(mod_id, None)
            return None
        return row

    def _row_rect(self, row: int) -> QRect:
        """返回指定行在视口坐标系中的完整横向区域。"""
        return QRect(0, self.rowViewportPosition(row), self.viewport().width(), self.rowHeight(row))

    def _refresh_feedback_row(self, mod_id: str) -> None:
        """为仍可定位的 Mod 同步状态单元格，并仅请求重绘对应行。"""
        row = self._row_for_mod(mod_id)
        if row is not None:
            self.refresh_mod_status(row)
            self.viewport().update(self._row_rect(row))

    def _sync_feedback_timer(self) -> None:
        """仅在表格可见且有需要推进的完成或动画反馈时运行共用定时器。"""
        needed = self.isVisible() and bool(self._completed or (self._progress and anim.is_enabled()))
        if needed and not self._feedback_timer.isActive():
            self._feedback_timer.start()
        elif not needed:
            self._feedback_timer.stop()

    def _advance_feedback(self) -> None:
        """清理过期成功提示并刷新视口内的活动行，再按剩余反馈决定定时器状态。"""
        now = monotonic()
        for mod_id in self._progress.keys() | self._completed.keys():
            if mod_id in self._completed and (self._completed[mod_id] <= now or not anim.is_enabled()):
                self._completed.pop(mod_id)
            row = self._row_for_mod(mod_id)
            if row is not None and not self.isRowHidden(row):
                rect = self._row_rect(row)
                if rect.intersects(self.viewport().rect()):
                    self.viewport().update(rect)
        self._sync_feedback_timer()

    def showEvent(self, event) -> None:
        """显示时同步可见行及反馈状态，并按动画设置启动逐行揭示。"""
        super().showEvent(event)
        self._sync_visible_rows()
        self._advance_feedback()
        self.reveal_visible_rows()

    def hideEvent(self, event) -> None:
        """隐藏时取消揭示效果和反馈定时器，避免不可见表格持续刷新。"""
        self.cancel_row_reveal()
        self._feedback_timer.stop()
        super().hideEvent(event)

    def paintEvent(self, event) -> None:
        """完成原生表格绘制后叠加可见 Mod 的部署进度、成功淡出或错误标记。"""
        self._sync_visible_rows()
        super().paintEvent(event)
        if not (self._progress or self._completed or self._errors):
            return
        painter = QPainter(self.viewport())
        painter.setClipRegion(event.region())
        header = self.horizontalHeader()
        # 按完整表格宽度计进度，横向滚动和拖动列宽时不会跳成另一个百分比。
        left, width = -header.offset(), header.length()
        now = monotonic()
        try:
            for mod_id in self._progress.keys() | self._completed.keys() | self._errors.keys():
                row = self._row_for_mod(mod_id)
                if row is None or self.isRowHidden(row):
                    continue
                row_rect = self._row_rect(row)
                if not row_rect.intersects(self.viewport().rect()):
                    continue
                line = QRectF(left, row_rect.bottom() - 1, width, 2)
                if mod_id in self._progress:
                    accent = QColor(theme.color("accent"))
                    track = QColor(accent)
                    track.setAlpha(35)
                    painter.fillRect(line, track)
                    progress = self._progress[mod_id]
                    if progress is None:
                        if anim.is_enabled():
                            segment = width * 0.22
                            x = left + ((now % 1.6) / 1.6) * (width + segment) - segment
                            fill = QRectF(x, line.top(), segment, line.height()).intersected(line)
                        else:
                            fill = line
                            accent.setAlpha(150)
                    else:
                        fill = QRectF(left, line.top(), width * progress, line.height())
                    painter.fillRect(fill, accent)
                elif mod_id in self._completed:
                    remaining = self._completed[mod_id] - now
                    if remaining > 0 and anim.is_enabled():
                        success = QColor(theme.color("success"))
                        success.setAlphaF(min(1.0, remaining / 0.7))
                        painter.fillRect(line, success)
                elif mod_id in self._errors:
                    painter.fillRect(
                        QRectF(left, row_rect.top() + 5, 3, max(0, row_rect.height() - 10)),
                        QColor(theme.color("danger")),
                    )
        finally:
            painter.end()
