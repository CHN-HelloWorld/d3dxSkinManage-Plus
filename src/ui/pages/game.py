"""「游戏」页 —— 独立顶级页：卡片式增删改 + 预览图 + 激活。

有游戏或正在新建时，左=卡片/列表视图，右=可调宽的 GameDetailPanel（不可拖到隐藏）。
没有游戏时显示全宽引导页，点击添加后恢复编辑界面。
激活游戏 = 设置 GameService.current_game_id 并发 game_changed，全局生效。
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from pathlib import Path

import shiboken6
from PySide6.QtCore import QPoint, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QDragEnterEvent, QDragMoveEvent, QDropEvent, QIcon, QPainter
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSlider,
    QSplitter,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src.core.config import Config
from src.core.game_service import GameConfig, GameService
from src.ui import anim, icons, theme
from src.ui.error_messages import user_error_message
from src.ui.smooth_scroll import SmoothScroller
from src.ui.widgets.game_card import GAME_ID_MIME, GameCard
from src.ui.widgets.game_detail_panel import GameDetailPanel
from src.utils.i18n import t as tr
from src.utils.logger import logger
from src.utils.paths import get_cache_dir, get_data_dir, is_image


def _cleanup_clipboard_temp(path: str) -> None:
    """清理剪贴板图片暂存目录中的文件。

    仅处理 cache/clipboard 内的路径，保留用户选择的外部原图；删除失败时跳过。
    """
    try:
        p = Path(path).resolve()
        clip_dir = (get_cache_dir() / "clipboard").resolve()
        if clip_dir in p.parents and p.is_file():
            p.unlink(missing_ok=True)
    except OSError:
        pass


class _EmptyGameArt(QWidget):
    """绘制随主题变化的游戏图标和柔和椭圆背景。"""

    def __init__(self) -> None:
        """为无游戏状态的装饰图预留固定尺寸。"""
        super().__init__()
        self.setFixedSize(120, 94)

    def paintEvent(self, event) -> None:
        """按当前主题绘制空库背景和游戏图标，并保持高分屏图标清晰。"""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(theme.color("accent.subtle")))
        painter.drawEllipse(QRectF(3, 7, 114, 87))
        ratio = self.devicePixelRatioF()
        symbol = icons.pixmap("actions/game", round(65 * ratio), "accent")
        symbol.setDevicePixelRatio(ratio)
        painter.drawPixmap(QPoint(27, 14), symbol)


class _CardGrid(QWidget):
    """卡片网格容器：承载 GameCard，接收长按拖拽排序的落点，以及外部图片拖入换预览图。"""

    def __init__(
        self, on_reorder, on_blank=None, on_image_drop=None, on_context=None, parent: QWidget | None = None
    ) -> None:
        """建立游戏卡片网格，保存排序、空白点击、预览图片拖入及右键菜单回调。"""
        super().__init__(parent)
        self._on_reorder = on_reorder
        self._on_blank = on_blank
        self._on_image_drop = on_image_drop  # 图片回调接收游戏标识和本地图片路径。
        self._on_context = on_context  # 右键菜单回调接收全局位置和命中卡片，空白区域传 None。
        self._drop_hover: GameCard | None = None
        self.setAcceptDrops(True)
        self.grid = QGridLayout(self)
        self.grid.setSpacing(12)
        self.grid.setContentsMargins(12, 12, 12, 12)
        self.grid.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)

    def mousePressEvent(self, e) -> None:
        """将落在网格空白区域的左键点击交给取消选择回调。"""
        # 卡片自行接收点击，传到容器的左键事件用于取消选择。
        if self._on_blank and e.button() == Qt.MouseButton.LeftButton:
            self._on_blank()
        super().mousePressEvent(e)

    def contextMenuEvent(self, e) -> None:
        """定位右键命中的游戏卡片，将卡片或空白区域及全局坐标交给菜单回调。"""
        # 按右键位置分别显示游戏菜单或空白区域菜单。
        if self._on_context is not None:
            card = self._card_at(e.pos())
            self._on_context(e.globalPos(), card)
            e.accept()
            return
        super().contextMenuEvent(e)

    def _card_at(self, pos) -> GameCard | None:
        """命中测试：返回落点 pos 下的可见 GameCard（无则 None）。"""
        for i in range(self.grid.count()):
            w = self.grid.itemAt(i).widget()
            if isinstance(w, GameCard) and w.isVisible() and w.geometry().contains(pos):
                return w
        return None

    def _clear_drop_hover(self) -> None:
        """移除当前图片拖放目标卡片的高亮并清空引用。"""
        if self._drop_hover is not None:
            self._drop_hover.set_drop_target(False)
            self._drop_hover = None

    def dragEnterEvent(self, e: QDragEnterEvent) -> None:
        """仅检查拖放格式以接受内部排序或外部文件，进入阶段不解析文件 URL 列表。"""
        # 接受两类拖入：游戏卡片排序（GAME_ID_MIME）/ 外部图片文件（hasUrls）。
        # 仅看格式不读 urls，避免大量文件时 IPC 卡顿。
        if e.mimeData().hasFormat(GAME_ID_MIME) or e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dragMoveEvent(self, e: QDragMoveEvent) -> None:
        """接受内部卡片排序移动，外部文件移动时仅高亮当前命中的游戏卡片。"""
        mime = e.mimeData()
        if mime.hasFormat(GAME_ID_MIME):
            e.acceptProposedAction()
            return
        if mime.hasUrls():
            e.acceptProposedAction()
            # 高亮光标下的游戏卡片，提示图片将作为该游戏的预览。
            card = self._card_at(e.position().toPoint())
            if card is not self._drop_hover:
                self._clear_drop_hover()
                if card is not None:
                    card.set_drop_target(True)
                    self._drop_hover = card
            return
        e.ignore()

    def dragLeaveEvent(self, e) -> None:
        """拖放离开网格时清除目标卡片高亮。"""
        self._clear_drop_hover()
        super().dragLeaveEvent(e)

    def dropEvent(self, e: QDropEvent) -> None:
        """内部卡片拖放交给排序回调，外部拖放只将首个有效本地图片交给目标游戏。"""
        mime = e.mimeData()
        # 处理内部游戏卡片的拖放排序。
        if mime.hasFormat(GAME_ID_MIME):
            drag_id = bytes(mime.data(GAME_ID_MIME)).decode("utf-8")
            self._on_reorder(drag_id, e.position().toPoint())
            e.acceptProposedAction()
            return
        # 将外部图片拖放到的卡片作为预览修改目标。
        self._clear_drop_hover()
        if self._on_image_drop and mime.hasUrls():
            card = self._card_at(e.position().toPoint())
            if card is not None and card.game_id:
                for url in mime.urls():
                    if url.isLocalFile() and is_image(url.toLocalFile()):
                        self._on_image_drop(card.game_id, url.toLocalFile())
                        e.acceptProposedAction()
                        return
        e.ignore()


class GamePage(QWidget):
    """游戏管理页"""

    game_changed = Signal(str)  # 激活游戏变化（""=无激活），主窗据此刷新上下文+门禁
    games_changed = Signal()  # 游戏增删改/排序
    deploy_dir_cleared = Signal(str)  # 已清空该游戏的部署目录（Mod 状态已转禁用）
    deployment_directory_selected = Signal(str)  # 重新选择并保存部署目录。

    GRID_BASE_W = 180
    GRID_BASE_H = 210

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        save_context: Callable[[GameConfig | None, GameConfig], AbstractContextManager] | None = None,
    ) -> None:
        """恢复游戏卡片缩放与视图偏好，建立草稿和选择状态，以及重排和配置写入的防抖定时器。"""
        super().__init__(parent)
        self._svc = GameService.instance()
        self._save_context = save_context
        self._cards: list[GameCard] = []
        self._display_ids: list[str] = []
        self._selected_id = ""
        # 卡片缩放：从配置恢复上次的百分比（70~150）→ 比例（持久化缩放）
        _pct = max(70, min(150, int(Config.instance().get("ui.game_card_scale", 100))))
        self._scale = _pct / 100.0
        self._new_pending = False  # 详情栏正在编辑一个尚未保存的新游戏
        self._prompting_unsaved = False  # 防止 QMessageBox 自身事件造成重复提示
        self._draft_card: GameCard | None = None  # 尚未添加完成的游戏显示为虚线草稿卡片。
        self._draft_game: GameConfig | None = None  # 尚未保存的草稿游戏。
        # 保存卡片或列表视图模式。
        _vm = Config.instance().get("ui.game_view_mode", "grid")
        self._view_mode = _vm if _vm in ("grid", "list") else "grid"
        self._cur_cols = 0  # 当前网格列数（仅列数变化才重排，避免抖动）
        # viewport 尺寸变化 → 防抖后重排（必须延迟：同步重排会改内容尺寸 → 触发滚动条
        # 切换 → 又一次 viewport Resize → 同步递归 → 栈溢出崩溃）
        self._reflow_timer = QTimer(self)
        self._reflow_timer.setSingleShot(True)
        self._reflow_timer.setInterval(60)
        self._reflow_timer.timeout.connect(self._reflow_if_cols_changed)
        # 缩放值持久化防抖：滑块停 250ms 后写一次配置，避免拖动时频繁写盘
        self._scale_persist = QTimer(self)
        self._scale_persist.setSingleShot(True)
        self._scale_persist.setInterval(250)
        self._scale_persist.timeout.connect(self._persist_scale)
        self._restoring_col_widths = False  # 恢复列宽时禁止 sectionResized 将配置重新写回。
        self._setup_ui()
        self.reload()
        # 应用已保存的视图模式，并同步选择框。
        if self._view_mode == "list":
            idx = self._view_combo.findData("list")
            if idx >= 0:
                self._view_combo.blockSignals(True)
                self._view_combo.setCurrentIndex(idx)
                self._view_combo.blockSignals(False)
            self._set_view("list")

    # UI
    def _setup_ui(self) -> None:
        """构建搜索工具栏、卡片与列表视图、空库入口和游戏详情栏，连接编辑、拖放及布局事件。"""
        root = QVBoxLayout(self)
        # 底部留 12px：避免列表视图/右侧详情栏底边紧贴窗口底（与总览页 12px 下边距一致）。
        root.setContentsMargins(0, 0, 0, 12)
        root.setSpacing(0)

        tb = QHBoxLayout()
        self._toolbar = tb
        tb.setContentsMargins(14, 10, 14, 10)
        tb.setSpacing(8)
        self._page_title = QLabel(tr("游戏"))
        tb.addWidget(self._page_title)
        tb.addStretch()

        self._search = QLineEdit()
        self._search.setPlaceholderText(tr("搜索游戏…"))
        self._search.setClearButtonEnabled(True)
        self._search.setFixedWidth(180)
        self._search_action = self._search.addAction(
            icons.get_icon("actions/search"), QLineEdit.ActionPosition.LeadingPosition
        )
        icons.apply_icon(self._search_action, "actions/search")  # 登记图标以参与主题刷新。
        self._search.textChanged.connect(lambda _t: self._apply_filter())
        tb.addWidget(self._search)

        self._view_combo = QComboBox()
        self._view_combo.addItem(icons.get_icon("actions/card-view"), tr("卡片视图"), "grid")
        self._view_combo.addItem(icons.get_icon("actions/list-view"), tr("列表视图"), "list")
        icons.apply_combo_item_icon(self._view_combo, 0, "actions/card-view")
        icons.apply_combo_item_icon(self._view_combo, 1, "actions/list-view")
        self._view_combo.setToolTip(tr("切换 卡片 / 列表 视图"))
        self._view_combo.currentIndexChanged.connect(lambda _i: self._set_view(self._view_combo.currentData()))
        tb.addWidget(self._view_combo)

        self._size_label = QLabel()
        # 缩放滑块左侧用放大镜（actions/search）表意「大小/缩放」，与总览页一致；
        # 勿用 card-view（那是视图切换语义）。
        icons.apply_pixmap(self._size_label, "actions/search", 16, "text.tertiary")
        self._size_slider = QSlider(Qt.Orientation.Horizontal)
        self._size_slider.setRange(70, 150)
        self._size_slider.setValue(int(self._scale * 100))  # 恢复持久化的缩放值
        self._size_slider.setFixedWidth(100)
        self._size_slider.setToolTip(tr("调整卡片大小"))
        self._size_slider.valueChanged.connect(self._on_scale)
        tb.addWidget(self._size_label)
        tb.addWidget(self._size_slider)

        self._add_btn = QPushButton(tr("添加游戏"))
        icons.apply_icon(self._add_btn, "actions/add", "text.on_accent")
        self._add_btn.setProperty("accent", True)
        self._add_btn.clicked.connect(self._on_add)
        tb.addWidget(self._add_btn)
        root.addLayout(tb)

        self._splitter = QSplitter(Qt.Orientation.Horizontal)

        self._views = QStackedWidget()
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        # 透明滚动区：卡片直接浮在背景图上
        self._scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        # 不出横向滚动条：宽度变化时靠 _reflow 重排列数（见 viewport 事件过滤器）
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._grid_container = _CardGrid(
            self._reorder_drop, self._deselect, self._on_card_image_dropped, on_context=self._on_grid_context_menu
        )
        self._scroll.setWidget(self._grid_container)
        # 滚轮平滑缓动（与商店/画廊/总览一致的柔和滚动手感）
        SmoothScroller.attach(self._scroll)
        # 拖动详情栏分隔条 → 左侧 viewport 变宽/变窄时重排卡片（GamePage.resizeEvent
        # 不会因 splitter 拖动触发，故监听 viewport 自身的 Resize）
        self._scroll.viewport().installEventFilter(self)
        self._views.addWidget(self._scroll)

        self._table = QTableWidget(0, 3)
        self._table.setHorizontalHeaderLabels([tr("游戏名称"), tr("英文名称"), tr("可执行文件")])
        _hdr = self._table.horizontalHeader()
        # 列宽可手动调整并持久化，末列伸展以填满剩余宽度。
        _hdr.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        _hdr.setStretchLastSection(True)  # 视觉上铺满；用户拖动后 sectionResized 记录
        _hdr.setMinimumSectionSize(60)  # 最小列宽，防拖到 0
        self._restore_col_widths()
        _hdr.sectionResized.connect(self._on_col_resized)
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        # 列表行和空白区域使用不同的右键菜单。
        self._table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._on_table_context_menu)
        # 列表视图内容区亚克力（与卡片视图一致）：内联样式才能染到视口
        self._apply_table_style()
        self._table.cellClicked.connect(self._on_row_clicked)
        self._table.cellDoubleClicked.connect(self._on_row_double)
        # 列表空白处点击 → 取消选中
        self._table.viewport().installEventFilter(self)
        self._views.addWidget(self._table)

        # 对应分类预设预览的「尚未添加游戏」场景，空库只保留一个添加入口。
        self._empty_page = QWidget()
        # 为空状态页显式设置箭头光标，避免继承已删除卡片或祖先的交互光标。
        self._empty_page.setCursor(Qt.CursorShape.ArrowCursor)
        _ep = QVBoxLayout(self._empty_page)
        _ep.setContentsMargins(30, 0, 30, 60)
        _ep.setAlignment(Qt.AlignmentFlag.AlignCenter)
        _ep.setSpacing(0)
        self._empty_art = _EmptyGameArt()
        _ep.addWidget(self._empty_art, 0, Qt.AlignmentFlag.AlignHCenter)
        _ep.addSpacing(21)
        _tip = QLabel(tr("你的游戏库还是空的"))
        _tip.setAlignment(Qt.AlignmentFlag.AlignCenter)
        _ep.addWidget(_tip)
        self._empty_tip = _tip
        _ep.addSpacing(23)
        self._empty_add_btn = QPushButton(tr("添加游戏"))
        self._empty_add_btn.setMinimumHeight(34)
        icons.apply_icon(self._empty_add_btn, "actions/add", "text.on_accent")
        self._empty_add_btn.setProperty("accent", True)
        self._empty_add_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._empty_add_btn.clicked.connect(self._on_add)
        _ep.addWidget(self._empty_add_btn, 0, Qt.AlignmentFlag.AlignHCenter)
        self._apply_empty_state_style()
        self._views.addWidget(self._empty_page)

        self._splitter.addWidget(self._views)

        self._detail = GameDetailPanel()
        self._detail.save_requested.connect(self._on_save)
        self._detail.delete_requested.connect(self._on_delete)
        self._detail.activate_requested.connect(self._on_activate)
        self._detail.preview_changed.connect(self._on_preview_changed)
        self._detail.exit_requested.connect(self._on_draft_exit)  # 退出草稿编辑。
        self._splitter.addWidget(self._detail)

        self._splitter.setStretchFactor(0, 1)
        self._splitter.setStretchFactor(1, 0)
        self._splitter.setCollapsible(1, False)  # 详情栏不可拖到隐藏
        # 详情栏保持保存的宽度，左侧区域吸收窗口尺寸变化。
        _dw = max(GameDetailPanel.MIN_WIDTH, int(Config.instance().get("ui.game_detail_width", 320)))
        self._splitter.setSizes([720, _dw])
        self._splitter.splitterMoved.connect(self._on_splitter_moved)
        root.addWidget(self._splitter, 1)

    # 数据与渲染。
    def _on_splitter_moved(self, _pos: int, _index: int) -> None:
        """保存详情栏宽度，供下次启动恢复。"""
        sizes = self._splitter.sizes()
        if len(sizes) >= 2 and sizes[1] > 0:
            Config.instance().set("ui.game_detail_width", int(sizes[1]))

    def _apply_table_style(self) -> None:
        """列表视图表格内联底色（随主题）。抽出以便换肤时重新着色。"""
        self._table.setStyleSheet(
            f"QTableWidget {{ background-color: {theme.surface_color('bg.card', 'list')};"
            f" alternate-background-color: {theme.surface_color('bg.subtle', 'list')}; }}"
        )

    def _apply_empty_state_style(self) -> None:
        """刷新空库提示文字颜色，并请求重绘主题相关的装饰图。"""
        self._empty_tip.setStyleSheet(f"color: {theme.color('text.primary')}; font-size: 18px; font-weight: 600;")
        self._empty_art.update()

    def retheme(self) -> None:
        """主题变化时刷新表格及卡片的内联颜色。

        这些颜色不随全局样式自动变化，须显式更新或重新创建相关控件。
        """
        try:
            self._apply_table_style()
            self._apply_empty_state_style()
            self.reload()
        except Exception:
            pass

    def reload(self) -> None:
        """重建游戏卡片和列表，恢复仍存在的选择或未保存草稿，并同步空库视图。"""
        games = self._svc.games
        self._display_ids = [g.id for g in games]
        self._render_cards(games)
        self._render_table(games)
        self._sync_view_page()
        if self._selected_id and self._selected_id in self._display_ids:
            self._select(self._selected_id)
        elif not self._new_pending:
            self._selected_id = ""
            self._detail.set_game(None)

    _EMPTY_PAGE_INDEX = 2

    def _sync_view_page(self) -> None:
        """无游戏且没有新建草稿时显示引导页，否则显示当前视图。"""
        empty = not self._svc.games and not self._new_pending
        if empty:
            self._views.setCurrentIndex(self._EMPTY_PAGE_INDEX)
        else:
            self._views.setCurrentIndex(0 if self._view_mode == "grid" else 1)
        self._detail.setVisible(not empty)
        for control in (self._search, self._view_combo, self._add_btn):
            control.setVisible(not empty)
        for control in (self._size_slider, self._size_label):
            control.setVisible(not empty and self._view_mode == "grid")
        self._page_title.setStyleSheet(
            "font-size: 24px; font-weight: 600;" if empty else "font-size: 18px; font-weight: bold;"
        )
        margins = (30, 26, 30, 23) if empty else (14, 10, 14, 10)
        self._toolbar.setContentsMargins(*margins)

    def _calc_cols(self) -> int:
        """根据视口宽度与卡片缩放计算网格列数，视口尚未就绪时使用初始列数。"""
        vpw = self._scroll.viewport().width()
        cell = int(self.GRID_BASE_W * self._scale) + 12
        return max(1, vpw // cell) if vpw > 40 else 4

    def _render_cards(self, games) -> None:
        """释放旧卡片并按当前顺序创建游戏卡片，恢复激活和选择状态，必要时在末尾添加草稿卡。"""
        for c in self._cards:
            c.setParent(None)
            c.deleteLater()
        self._cards = []
        while self._grid_container.grid.count():
            it = self._grid_container.grid.takeAt(0)
            w = it.widget()
            if w:
                w.setParent(None)
        cols = self._calc_cols()
        self._cur_cols = cols
        cw = int(self.GRID_BASE_W * self._scale)
        ch = int(self.GRID_BASE_H * self._scale)
        active = self._svc.current_game_id if self._svc.has_active_game() else ""
        self._draft_card = None
        for i, g in enumerate(games):
            card = GameCard(g, card_width=cw, card_height=ch)
            card.set_active(g.id == active)
            card.set_selected(g.id == self._selected_id)
            card.clicked.connect(self._select)
            card.double_clicked.connect(self._on_activate)
            self._cards.append(card)
            self._grid_container.grid.addWidget(card, i // cols, i % cols)
        # 在末尾追加选中的草稿卡片；草稿没有游戏编号，不允许拖动或启用。
        if self._new_pending:
            draft = GameCard(GameConfig(name=tr("新游戏")), card_width=cw, card_height=ch)
            draft.set_draft(True)
            draft.set_selected(True)
            self._cards.append(draft)
            self._draft_card = draft
            n = len(games)
            self._grid_container.grid.addWidget(draft, n // cols, n % cols)
        self._apply_filter()

    def _render_table(self, games) -> None:
        """重建游戏名称、英文名和启动路径列表，未保存草稿使用无游戏标识的末行。"""
        n = len(games)
        # 列表末尾追加草稿占位行。
        self._table.setRowCount(n + (1 if self._new_pending else 0))
        for i, g in enumerate(games):
            name_item = QTableWidgetItem(g.name)
            name_item.setData(Qt.ItemDataRole.UserRole, g.id)
            self._table.setItem(i, 0, name_item)
            self._table.setItem(i, 1, QTableWidgetItem(g.name_en))
            self._table.setItem(i, 2, QTableWidgetItem(g.exe_path))
        if self._new_pending:
            from PySide6.QtGui import QColor, QFont

            accent = QColor(theme.color("accent"))
            font = QFont()
            font.setItalic(True)
            labels = [tr("＋ 新游戏（未确认）"), "", ""]
            for col, txt in enumerate(labels):
                it = QTableWidgetItem(txt)
                it.setData(Qt.ItemDataRole.UserRole, "")  # 空 id 标记草稿行
                it.setForeground(accent)
                it.setFont(font)
                self._table.setItem(n, col, it)
            self._table.selectRow(n)

    def _reflow(self, animated: bool = False) -> None:
        """按显示顺序和当前列数重新放置卡片，保留末尾草稿并可从原位置播放重排动画。"""
        if not self._cards:
            return
        cols = self._calc_cols()
        self._cur_cols = cols
        old_pos = {c: c.pos() for c in self._cards} if animated else {}
        for c in self._cards:
            self._grid_container.grid.removeWidget(c)
        ordered: list = []
        for i, cid in enumerate(self._display_ids):
            card = next((c for c in self._cards if c.game_id == cid), None)
            if card:
                self._grid_container.grid.addWidget(card, i // cols, i % cols)
                ordered.append(card)
        # 草稿未加入显示编号列表，单独放在末尾。
        if self._draft_card is not None:
            n = len(ordered)
            self._grid_container.grid.addWidget(self._draft_card, n // cols, n % cols)
            ordered.append(self._draft_card)
        if animated and anim.is_enabled():
            # 列数变化时先由布局计算卡片目标坐标，再从当前位置执行平滑位移动画。
            self._grid_container.grid.activate()
            for c in ordered:
                old_p = old_pos.get(c)
                new_p = c.pos()
                if old_p is not None and old_p != new_p:
                    c.move(old_p)
                    anim.animate_move(c, new_p)

    def resizeEvent(self, e) -> None:
        """卡片模式下仅在列数变化时重排，普通宽度变化由布局系统处理。"""
        super().resizeEvent(e)
        if self._view_mode == "grid" and self._calc_cols() != self._cur_cols:
            # 仅列数变化时重排；同列数的宽度变化由布局处理，避免逐像素移除和添加控件。
            self._reflow(animated=True)

    # 未保存配置守卫
    def has_unsaved_changes(self) -> bool:
        """详情栏当前是否有尚未保存的游戏配置。"""
        return bool(self._detail.has_unsaved_changes())

    def confirm_pending_changes(self) -> bool:
        """离开当前游戏详情前询问保存。

        返回 True 表示可以继续原操作；False 表示用户取消或保存失败，应留在当前界面。
        """
        if not self.has_unsaved_changes():
            return True
        if self._prompting_unsaved:
            return False

        fields = self._detail.collect()
        game_name = fields.get("name") or tr("当前游戏")
        self._prompting_unsaved = True
        try:
            answer = QMessageBox.question(
                self,
                tr("游戏配置未保存"),
                tr("游戏「{game_name}」的配置尚未保存。\n是否现在保存？", game_name=game_name),
                (
                    QMessageBox.StandardButton.Save
                    | QMessageBox.StandardButton.Discard
                    | QMessageBox.StandardButton.Cancel
                ),
                QMessageBox.StandardButton.Save,
            )
        finally:
            self._prompting_unsaved = False

        if answer == QMessageBox.StandardButton.Save:
            return self._on_save(fields)
        if answer == QMessageBox.StandardButton.Discard:
            self._discard_pending_changes()
            return True
        return False

    def _discard_pending_changes(self) -> None:
        """放弃详情栏修改并恢复最近一次已保存内容。"""
        if self._new_pending:
            self._exit_draft(silent=True)
            return
        if not self._selected_id:
            self._detail.set_game(None)
            return
        game = self._svc.get_game(self._selected_id)
        is_active = self._selected_id == self._svc.current_game_id and self._svc.has_active_game()
        self._detail.set_game(game, is_active=is_active)

    # 选择状态。
    def _select(self, game_id: str) -> None:
        """确认当前未保存修改后切换所选游戏，退出草稿并同步详情和卡片选中状态。"""
        # 点击详情栏之外的游戏卡/列表行会离开当前编辑对象：有修改时先询问保存。
        if self.has_unsaved_changes() and not self.confirm_pending_changes():
            return
        if self._new_pending:
            self._exit_draft(silent=True)
        self._selected_id = game_id
        g = self._svc.get_game(game_id)
        is_active = game_id == self._svc.current_game_id and self._svc.has_active_game()
        self._detail.set_game(g, is_active=is_active)
        for c in self._cards:
            c.set_selected(c.game_id == game_id)

    def _on_row_clicked(self, row: int, _col: int) -> None:
        """将列表行选择映射到游戏标识，点击当前草稿行时继续保留草稿编辑。"""
        item = self._table.item(row, 0)
        if item:
            gid = item.data(Qt.ItemDataRole.UserRole)
            if self._new_pending and not gid:
                return  # 点击草稿自身时保持编辑状态。
            self._select(gid)

    def _on_row_double(self, row: int, _col: int) -> None:
        """将有效列表行的双击交给对应游戏的激活切换流程。"""
        item = self._table.item(row, 0)
        if item:
            self._on_activate(item.data(Qt.ItemDataRole.UserRole))

    def _deselect(self) -> None:
        """点击空白区域 → 预览/详情栏回到未选中空状态。"""
        if self.has_unsaved_changes() and not self.confirm_pending_changes():
            return
        if self._new_pending:
            self._exit_draft(silent=True)
            return
        if not self._selected_id:
            return
        self._selected_id = ""
        self._detail.set_game(None)
        for c in self._cards:
            c.set_selected(False)
        self._table.clearSelection()

    def eventFilter(self, obj, event):
        """处理列表空白点击和卡片视口尺寸变化，延后重排以避免布局事件递归。"""
        from PySide6.QtCore import QEvent

        # 安装过滤器时控件可能尚未全部初始化，先检查引用以免虚函数回调访问不存在的属性。
        tbl = getattr(self, "_table", None)
        scr = getattr(self, "_scroll", None)
        if tbl is not None and obj is tbl.viewport() and event.type() == QEvent.Type.MouseButtonPress:
            if tbl.itemAt(event.pos()) is None:
                self._deselect()
        elif (
            scr is not None
            and obj is scr.viewport()
            and event.type() == QEvent.Type.Resize
            and self._view_mode == "grid"
        ):
            # 尺寸变化时延迟重排，避免同步重排再次触发尺寸事件。
            self._reflow_timer.start()
        return super().eventFilter(obj, event)

    def _reflow_if_cols_changed(self) -> None:
        """仅当列数因 viewport 宽度变化而改变时才重排（拖动详情栏分隔条等场景）。"""
        if self._view_mode != "grid" or not self._cards:
            return
        new_cols = self._calc_cols()
        if new_cols != self._cur_cols:
            self._reflow(animated=True)

    # 游戏增删改查与激活。
    def _on_add(self, _checked: bool = False) -> None:
        """处理现有未保存修改后建立新游戏草稿，显示占位卡片并在列表模式下滚动至草稿行。"""
        if self.has_unsaved_changes() and not self.confirm_pending_changes():
            return
        # 先退出已有草稿，再显示新草稿及确认、退出操作；确认保存后才写入数据。
        if self._new_pending:
            # 创建新草稿前先处理已有草稿，未保存内容按退出流程确认。
            if not self._exit_draft():
                return
        self._new_pending = True
        self._selected_id = ""
        self.reload()  # 渲染末尾虚线草稿卡
        self._detail.set_game(GameConfig(), is_active=False, draft=True)
        # 列表视图下滚动到草稿行（末行）并选中，卡片视图靠 reload 的草稿卡呈现
        if self._view_mode == "list" and self._table.rowCount() > 0:
            self._table.scrollToBottom()

    def _on_draft_exit(self) -> None:
        """尝试退出草稿，已有输入时先确认是否放弃。"""
        self._exit_draft()

    def _exit_draft(self, silent: bool = False) -> bool:
        """退出并丢弃草稿，成功返回 True，取消退出返回 False。

        已有输入时先确认；silent 用于保存成功后的草稿清理，直接退出。
        """
        if not self._new_pending:
            return True
        if not silent and self._detail.has_unsaved_input():
            ok = QMessageBox.question(
                self,
                tr("放弃新游戏"),
                tr("尚未确认添加，填写的信息将不会保存。\n确定退出吗？"),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if ok != QMessageBox.StandardButton.Yes:
                return False
        self._new_pending = False
        self._selected_id = ""
        self.reload()  # 移除草稿卡/草稿行
        self._detail.set_game(None)  # 详情栏回空状态
        self._table.clearSelection()
        return True

    @staticmethod
    def _effective_deploy_dir(game: GameConfig | None) -> Path | None:
        """按主窗口规则读取用户显式配置的部署目录，不从加载器路径推导。"""
        if game is None:
            return None
        if (game.mods_path or "").strip():
            return Path(game.mods_path.strip())
        return None

    @staticmethod
    def _path_key(path: Path) -> str:
        """按绝对路径与平台大小写规则生成比较键，不解析符号链接。"""
        return os.path.normcase(os.path.abspath(os.fspath(path)))

    def _confirm_migoto_path_cleared(
        self,
        old_game: GameConfig | None,
        new_game: GameConfig,
    ) -> bool:
        """清空加载器路径前说明对变体记录的影响。

        该路径用于定位用户 INI 和隐式命名空间。确认后停用相关记录，
        取消时仅恢复此项配置，保留其他字段的编辑结果。
        """
        old_path = (old_game.migoto_path or "").strip() if old_game else ""
        if not old_path or (new_game.migoto_path or "").strip():
            return True
        if not self._deploy_dir_has_data(new_game.id):
            return True

        answer = QMessageBox.question(
            self,
            tr("清空 3DMigoto 路径"),
            tr(
                "当前部署目录内还存在需要记录变体的Mod，您确定要删除此项配置吗？\n\n"
                "选择“是”后，程序将不再记录这些 Mod 在游戏内切换的变体状态。\n"
                "选择“否”将取消对 3DMigoto 路径的本次更改，其余修改照常保存。"
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            new_game.migoto_path = old_path
        return True

    def _deploy_dir_has_data(self, game_id: str) -> bool:
        """该游戏是否还有已启用（已部署）的 Mod。"""
        try:
            from src.core.mod_manager import ModManager

            return ModManager().variants.has_deployed_mods(game_id)
        except Exception:
            return True

    def _confirm_deploy_dir_migration(
        self,
        old_game: GameConfig | None,
        new_game: GameConfig,
        *,
        moved_paths: list[tuple[Path, Path]] | None = None,
    ) -> bool:
        """部署目录变化时询问处置；返回是否可继续保存。

        两种情况：清空「Mod 文件夹」→ 问是否同时清空原目录（见 `_confirm_deploy_dir_cleared`）；
        改指另一个目录 → 问是否迁移旧目录内容。
        """
        old_dir = self._effective_deploy_dir(old_game)
        new_dir = self._effective_deploy_dir(new_game)
        if new_dir is None:
            return self._confirm_deploy_dir_cleared(old_game, new_game, old_dir)
        if old_dir is None:
            return True
        if self._path_key(old_dir) == self._path_key(new_dir):
            return True
        try:
            has_old_data = old_dir.is_dir() and any(old_dir.iterdir())
        except OSError:
            has_old_data = False
        if not has_old_data:
            return True

        answer = QMessageBox.question(
            self,
            tr("迁移已有部署"),
            tr(
                (
                    "检测到 Mod 部署目录已经更改：\n\n旧目录：{old_dir}\n"
                    "新目录：{new_dir}\n\n是否将旧目录内的全部文件和子文件夹迁移到新目录？"
                    "包括手动放入的文件。\n选择“否”将只保存新配置，旧目录内容保持不变。"
                ),
                old_dir=old_dir,
                new_dir=new_dir,
            ),
            (QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No | QMessageBox.StandardButton.Cancel),
            QMessageBox.StandardButton.Yes,
        )
        if answer == QMessageBox.StandardButton.Cancel:
            return False
        if answer == QMessageBox.StandardButton.No:
            return True

        ok, error = self._migrate_deploy_contents(old_dir, new_dir, moved_paths=moved_paths)
        if not ok:
            QMessageBox.critical(
                self,
                tr("迁移部署失败"),
                tr("未能安全迁移部署数据，游戏配置尚未保存。\n\n{error}", error=user_error_message(error)),
            )
            return False
        # 物理内容已经在新目录了，库里记的 deployed_path 还指着旧目录 —— 必须立刻改写，
        # 否则部署态对账会把它判成「被外删」而置为禁用，随后 set_deploy_dir 的重登记
        # 因 is_enabled 为假整体跳过，ini 双向同步再也建不起来（改部署侧 ini 不回写）。
        try:
            from src.core.mod_manager import ModManager

            ModManager().rebase_deployments(new_game.id, old_dir, new_dir)
        except Exception as exc:
            logger.error("迁移后改写部署路径失败: %s", exc)
            raise OSError(tr("迁移后更新部署路径失败：{error}", error=exc)) from exc
        return True

    def _confirm_deploy_dir_cleared(
        self,
        old_game: GameConfig | None,
        new_game: GameConfig,
        old_dir: Path | None,
    ) -> bool:
        """用户清空了「Mod 文件夹」：原目录还有内容时问是否一并清空。

        「是」→ 清空原目录（已启用的 Mod 同时置为禁用）后保存清空的配置；
        「否」→ 本次不修改「Mod 文件夹」配置（其余字段照常保存），原目录保持不变。
        """
        if old_dir is None:
            return True
        try:
            has_data = old_dir.is_dir() and any(old_dir.iterdir())
        except OSError:
            has_data = False
        if not has_data:
            return True

        answer = QMessageBox.question(
            self,
            tr("清空部署目录"),
            tr(
                (
                    "当前未填写 Mod 文件夹，原目录仍有内容：\n\n{old_dir}\n\n"
                    "是否永久删除此目录内的全部文件和子文件夹？\n包括手动放入的文件，"
                    "删除后无法恢复。\n选择“否”将保留原目录内容和原 Mod 文件夹设置。"
                ),
                old_dir=old_dir,
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            new_game.mods_path = old_game.mods_path if old_game else ""
            return True

        from src.core.mod_manager import ModManager

        disabled, error = ModManager().clear_deploy_dir_contents(new_game.id, old_dir)
        if error:
            QMessageBox.critical(
                self,
                tr("清空部署目录失败"),
                tr("未能清空原有部署目录，配置未保存：{error}", error=user_error_message(error)),
            )
            return False
        # 卡片/角色计数仍显示清空前的启用态，必须让主窗重建视图（games_changed 只下发
        # 部署目录，不重建 Mod 视图）。
        self.deploy_dir_cleared.emit(new_game.id)
        QMessageBox.information(
            self, tr("清空部署目录"), tr("已清空原有部署目录，{disabled} 个 Mod 已置为禁用。", disabled=disabled)
        )
        return True

    def _migrate_deploy_contents(
        self, old_dir: Path, new_dir: Path, *, moved_paths: list[tuple[Path, Path]] | None = None
    ) -> tuple[bool, str]:
        """把旧部署目录的直接子项安全移动到新目录；冲突时不开始迁移。"""
        old_dir = Path(old_dir)
        new_dir = Path(new_dir)
        old_key = self._path_key(old_dir)
        new_key = self._path_key(new_dir)
        if old_key == new_key:
            return True, ""
        try:
            common = os.path.commonpath([old_key, new_key])
        except ValueError:  # 跨盘没有公共路径，允许 shutil.move 走复制+删除
            common = ""
        if common in (old_key, new_key):
            return False, tr("新旧部署目录不能互相嵌套，请选择彼此独立的目录。")
        if not old_dir.is_dir():
            return True, ""
        try:
            entries = list(old_dir.iterdir())
        except OSError as exc:
            return False, tr("无法读取旧部署目录：{exc}", exc=user_error_message(exc))
        if not entries:
            return True, ""
        if new_dir.exists() and not new_dir.is_dir():
            return False, tr("新部署路径已经存在，但它不是文件夹。")

        conflicts = [
            entry.name for entry in entries if (new_dir / entry.name).exists() or (new_dir / entry.name).is_symlink()
        ]
        if conflicts:
            shown = "、".join(conflicts[:8])
            if len(conflicts) > 8:
                shown += tr(" 等 {count} 项", count=len(conflicts))
            return False, tr("新目录中存在同名内容，未执行任何移动：{shown}", shown=shown)

        moved: list[tuple[Path, Path]] = []
        try:
            new_dir.mkdir(parents=True, exist_ok=True)
            for source in entries:
                target = new_dir / source.name
                shutil.move(str(source), str(target))
                moved.append((source, target))
        except Exception as exc:
            rollback_errors: list[str] = []
            for source, target in reversed(moved):
                try:
                    if target.exists() and not source.exists():
                        shutil.move(str(target), str(source))
                except Exception as rollback_exc:
                    rollback_errors.append(str(rollback_exc))
            detail = tr("移动过程中出错：{exc}", exc=user_error_message(exc))
            if rollback_errors:
                detail += tr("；部分内容回滚失败：") + "；".join(rollback_errors)
            return False, detail

        # 迁移仅搬运部署内容，保留原目录本身。
        if moved_paths is not None:
            moved_paths.extend(moved)
        logger.info("部署目录已迁移: %s -> %s（%d 项，旧目录保留）", old_dir, new_dir, len(moved))
        return True, ""

    def _rollback_deploy_contents(self, game_id: str, moved_paths: list[tuple[Path, Path]]) -> None:
        """配置未提交时仅还原本次移动的部署内容，保留目标目录原有文件及外部新增内容。"""
        if not moved_paths:
            return
        from src.core.mod_manager import ModManager, ResourceDirectoryMigrationError

        errors = []
        for source, target in reversed(moved_paths):
            try:
                if os.path.lexists(source):
                    raise FileExistsError(str(source))
                shutil.move(str(target), str(source))
            except OSError as exc:
                errors.append(str(exc))
        if not errors:
            try:
                source, target = moved_paths[0]
                ModManager().rebase_deployments(
                    game_id, target.parent, source.parent, entry_names={target.name for _, target in moved_paths}
                )
            except Exception as exc:
                errors.append(str(exc))
        if errors:
            resource_dir = self._svc.get_resource_dir(game_id)
            raise ResourceDirectoryMigrationError(resource_dir, get_data_dir() / resource_dir, errors)

    def _on_save(self, fields: dict) -> bool:
        """校验游戏名称并确认路径变更，保存配置和预览后更新编辑基线，成功时通知宿主刷新。"""
        # 保存前统一检查名称是否为空。
        if not (fields.get("name") or "").strip():
            QMessageBox.information(self, tr("提示"), tr("请先填写游戏名称。"))
            return False
        from src.core.mod_manager import ResourceDirectoryMigrationError

        cfg = GameConfig(
            id=fields["id"],
            name=fields["name"],
            name_en=fields["name_en"],
            exe_path=fields["exe_path"],
            migoto_path=fields["migoto_path"],
            mods_path=fields["mods_path"],
            preview=fields.get("preview", ""),
        )
        old_game = self._svc.get_game(cfg.id) if cfg.id else None
        migration_errors: list[str] = []
        try:
            with self._save_context(old_game, cfg) if self._save_context is not None else nullcontext():
                moved_paths: list[tuple[Path, Path]] = []
                try:
                    if not self._confirm_migoto_path_cleared(old_game, cfg):
                        return False
                    if not self._confirm_deploy_dir_migration(old_game, cfg, moved_paths=moved_paths):
                        return False
                    gid = self._svc.save_game(cfg, on_migration_error=migration_errors.append)
                except ResourceDirectoryMigrationError:
                    raise
                except (OSError, RuntimeError):
                    self._rollback_deploy_contents(cfg.id, moved_paths)
                    raise
                saved = self._svc.get_game(gid)
                src = saved.preview if saved else ""
                if src:
                    stored = self._persist_preview(gid, src)
                    if stored and stored != src:
                        saved.preview = stored
                        self._svc.save_game(saved, migrate_resources=False)
        except (OSError, RuntimeError) as exc:
            logger.error("保存游戏设置失败: %s", exc)
            message = str(exc)
            if isinstance(exc, ResourceDirectoryMigrationError):
                self._detail.set_game(self._svc.get_game(cfg.id), is_active=False)
                self.reload()
                message = tr(
                    "资源目录迁移和恢复未能完成，已停止此游戏的自动同步。\n\n资源位置：{path}\n\n{error}",
                    path=exc.actual_path,
                    error=exc,
                )
            QMessageBox.warning(self, tr("保存游戏设置失败"), message)
            return False
        self._new_pending = False
        self._selected_id = gid
        # reload 会重新调用 _select；先把详情栏更新为已保存基线，避免它把刚完成的保存
        # 再次识别成“未保存”并重复弹窗。
        saved_game = self._svc.get_game(gid)
        is_active = gid == self._svc.current_game_id and self._svc.has_active_game()
        self._detail.set_game(saved_game, is_active=is_active)
        self.reload()
        if fields.get("_mods_path_selected"):
            self.deployment_directory_selected.emit(gid)
        self.games_changed.emit()
        for error in migration_errors:
            QMessageBox.warning(self, tr("资源目录未迁移"), error)
        return True

    def _persist_preview(self, gid: str, src: str) -> str:
        """把用户选定的预览图复制进游戏资源目录，返回存储后的路径。失败回退原路径。"""
        try:
            if not (src and is_image(src) and Path(src).exists()):
                return ""
            dst_dir = get_data_dir() / self._svc.get_resource_dir(gid)
            dst_dir.mkdir(parents=True, exist_ok=True)
            ext = Path(src).suffix.lower() or ".png"
            dst = dst_dir / f"_game_preview{ext}"
            if Path(src).resolve() == dst.resolve():
                return str(dst)
            for old in dst_dir.glob("_game_preview.*"):
                try:
                    old.unlink()
                except OSError:
                    pass
            shutil.copy2(src, dst)
            # 剪贴板预览复制到游戏资源目录后删除其缓存副本，仅清理 cache/clipboard 内的暂存文件。
            _cleanup_clipboard_temp(src)
            return str(dst)
        except OSError as exc:
            logger.error("保存游戏预览图失败 %s: %s", gid, exc)
            return src

    def _clear_preview_file(self, gid: str) -> None:
        """删除游戏资源目录里的 _game_preview.*（移除/删除预览图时调用）。"""
        try:
            dst_dir = get_data_dir() / self._svc.get_resource_dir(gid)
            for old in dst_dir.glob("_game_preview.*"):
                old.unlink(missing_ok=True)
        except OSError:
            pass

    def _on_preview_changed(self, path: str) -> None:
        """预览图变更后立即更新已保存的游戏配置和对应卡片。

        尚未保存的新游戏仅保留暂存路径，提交表单时一起写入。
        """
        gid = self._selected_id
        if not gid or self._new_pending:
            return
        g = self._svc.get_game(gid)
        if not g:
            return
        if path:
            stored = self._persist_preview(gid, path)
        else:
            self._clear_preview_file(gid)
            stored = ""
        g.preview = stored
        try:
            self._svc.save_game(g, migrate_resources=False)
        except OSError as exc:
            self._detail.sync_preview_path(stored, persisted=False)
            QMessageBox.warning(self, tr("保存游戏设置失败"), str(exc))
            return
        # 同步详情中的预览路径，供取消选择后再次选中时恢复。
        self._detail.sync_preview_path(stored)
        # 就地刷新对应游戏卡片的预览，无需整页 reload（避免闪烁）
        for c in self._cards:
            if c.game_id == gid:
                c.set_game(g)
                break

    def _on_card_image_dropped(self, game_id: str, path: str) -> None:
        """外部图片拖到游戏卡片 → 持久化为该游戏预览图并就地刷新。

        与详情栏的「更换预览图」共用同一套持久化（_persist_preview）；无需先选中该卡片，
        直接对落点游戏生效，快速换图。
        """
        g = self._svc.get_game(game_id)
        if not g:
            return
        stored = self._persist_preview(game_id, path)
        if not stored:
            return
        g.preview = stored
        try:
            self._svc.save_game(g, migrate_resources=False)
        except OSError as exc:
            if game_id == self._selected_id:
                self._detail.sync_preview_path(stored, persisted=False)
            QMessageBox.warning(self, tr("保存游戏设置失败"), str(exc))
            return
        # 若该游戏正被详情栏选中，回填暂存路径，避免「取消选中再选回」走空分支（同 _on_preview_changed）
        if game_id == self._selected_id:
            self._detail.sync_preview_path(stored)
        for c in self._cards:
            if c.game_id == game_id:
                c.set_game(g)
                break

    def _on_delete(self, game_id: str) -> None:
        """确认删除后清理游戏关联数据与配置，通知宿主更新激活状态，并报告未完成的文件清理。"""
        if game_id == self._selected_id and self.has_unsaved_changes():
            if not self.confirm_pending_changes():
                return
        g = self._svc.get_game(game_id)
        if not g:
            return
        if not self._confirm_delete(g):
            return
        was_active = game_id == self._svc.current_game_id
        from src.core.mod_manager import ModManager

        stats = ModManager().purge_game_data(game_id, self._svc.get_resource_dir(game_id))
        errors = stats.get("errors") or []
        self._svc.delete_game(game_id)
        self._selected_id = ""
        self.reload()
        self.games_changed.emit()
        if was_active:
            self.game_changed.emit("")  # 当前游戏失效 → 主窗重算门禁
        if errors:
            from src.ui.dialogs.game_delete import GameDeleteReportDialog

            report = GameDeleteReportDialog(g.name, stats, self)
            try:
                report.exec()
            finally:
                report.deleteLater()

    def _confirm_delete(self, g: GameConfig) -> bool:
        """统一确认游戏删除范围，明确展示配置与数据的处理后果后再执行。"""
        from functools import partial

        from src.core.mod_manager import ModManager
        from src.ui.dialogs.game_delete import GameDeleteDialog, format_data_size

        manager = ModManager()
        dialog = GameDeleteDialog(
            g.name, None, self, scan=partial(manager.describe_game_data, g.id, self._svc.get_resource_dir(g.id))
        )
        try:
            accepted = dialog.exec() == QDialog.DialogCode.Accepted
            info = dict(dialog.info or {}) if accepted else {}
        finally:
            dialog.deleteLater()
        if not accepted:
            return False
        lib = info.get("library_dir") or ""
        has_data = bool(lib) or info.get("mods", 0) or info.get("deployed", 0)
        if not has_data:
            return True  # 空游戏没什么可丢的，不必再拦一道
        return (
            QMessageBox.question(
                self,
                tr("再次确认"),
                tr(
                    "确定永久删除「{name}」及其数据？资源目录占用 {size}。此操作无法撤销。",
                    name=g.name,
                    size=format_data_size(info.get("library_size", 0)),
                ),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            == QMessageBox.StandardButton.Yes
        )

    # 右键菜单。
    def _on_grid_context_menu(self, global_pos, card) -> None:
        """卡片视图右键：落在游戏卡上 → 启用（未激活时）/删除；落在空白 → 添加/切换视图。

        参数顺序须与 _CardGrid.contextMenuEvent 的回调约定一致：(global_pos, card)。
        """
        if card is not None and card.game_id:
            self._show_game_context_menu(card.game_id, global_pos)
        else:
            self._show_blank_context_menu(global_pos)

    def _on_table_context_menu(self, pos) -> None:
        """列表视图右键：落在游戏行 → 启用/删除；落在空行 → 添加/切换视图。"""
        item = self._table.itemAt(pos)
        global_pos = self._table.viewport().mapToGlobal(pos)
        gid = ""
        if item is not None:
            id_item = self._table.item(item.row(), 0)
            gid = id_item.data(Qt.ItemDataRole.UserRole) if id_item else ""
        if gid:
            self._show_game_context_menu(gid, global_pos)
        else:
            self._show_blank_context_menu(global_pos)

    def _show_game_context_menu(self, game_id: str, global_pos) -> None:
        """游戏卡/行右键菜单：启用此游戏（未激活时）+ 删除。"""
        g = self._svc.get_game(game_id)
        if not g:
            return
        actions = []
        is_active = self._svc.has_active_game() and game_id == self._svc.current_game_id
        if not is_active:
            actions.append((icons.get_icon("status/enabled"), tr("启用此游戏"), lambda: self._on_activate(game_id)))
            actions.append(None)
        actions.append((icons.get_icon("actions/remove", "danger"), tr("删除"), lambda: self._on_delete(game_id)))
        self._exec_context_menu(global_pos, actions)

    def _show_blank_context_menu(self, global_pos) -> None:
        """空白处右键菜单：添加游戏 + 卡片/列表视图切换。"""
        actions = [(icons.get_icon("actions/add"), tr("添加游戏"), self._on_add), None]
        if self._view_mode == "grid":
            actions.append((icons.get_icon("actions/list-view"), tr("切换到列表视图"), lambda: self._set_view("list")))
        else:
            actions.append((icons.get_icon("actions/card-view"), tr("切换到卡片视图"), lambda: self._set_view("grid")))
        self._exec_context_menu(global_pos, actions)

    def _exec_context_menu(self, global_pos, actions: list[tuple[QIcon, str, Callable[[], None]] | None]) -> None:
        """游戏及空白区域共用一个菜单，清空旧动作后再派发当前选择，允许回调安全重开。"""
        if getattr(self, "_context_menu_active", False):
            return
        menu = getattr(self, "_context_menu", None)
        if menu is None or not shiboken6.isValid(menu):
            menu = QMenu(self)
            self._context_menu = menu
        self._context_menu_active = True
        callbacks = {}
        callback = None
        try:
            menu.clear()
            for item in actions:
                if item is None:
                    menu.addSeparator()
                else:
                    icon, title, action = item
                    callbacks[menu.addAction(icon, title)] = action
            chosen = menu.exec(global_pos)
            callback = callbacks.get(chosen)
        finally:
            callbacks.clear()
            if shiboken6.isValid(menu):
                menu.clear()
            self._context_menu_active = False
        if callback is not None:
            callback()

    def _on_activate(self, game_id: str) -> None:
        """确认未保存修改后切换游戏激活状态；草稿需先保存，再次激活当前游戏则取消激活。"""
        if self.has_unsaved_changes() and not self.confirm_pending_changes():
            return
        if not game_id:
            # 未保存的新游戏：先存再激活
            if not self._new_pending:
                return
            fields = self._detail.collect()
            if not fields["name"]:
                QMessageBox.information(self, tr("提示"), tr("请先填写游戏名称再启用。"))
                return
            self._on_save(fields)
            game_id = self._selected_id
        if not game_id:
            return
        # 再次激活当前游戏会取消激活，使依赖游戏上下文的页面恢复未激活状态。
        if self._svc.has_active_game() and game_id == self._svc.current_game_id:
            self._svc.current_game_id = ""
            for c in self._cards:
                c.set_active(False)
            self._select(game_id)  # 仍选中、详情可见，但非激活态
            self.game_changed.emit("")  # 通知主窗重算门禁（锁住总览/画廊/商店）
            return
        self._svc.current_game_id = game_id
        self._select(game_id)
        for c in self._cards:
            c.set_active(c.game_id == game_id)
        self.game_changed.emit(game_id)

    # 视图、筛选与缩放。
    def _set_view(self, mode: str) -> None:
        """规范化并保存卡片或列表模式，同步选择控件，进入卡片模式时重排布局。"""
        if mode not in ("grid", "list"):
            mode = "grid"
        self._view_mode = mode
        self._sync_view_page()
        # 保存视图模式。
        try:
            Config.instance().set("ui.game_view_mode", mode)
        except Exception:
            pass
        # 恢复/程序改动时同步下拉框，且避免反复触发 _set_view。
        idx = self._view_combo.findData(mode)
        if idx >= 0 and self._view_combo.currentIndex() != idx:
            self._view_combo.blockSignals(True)
            self._view_combo.setCurrentIndex(idx)
            self._view_combo.blockSignals(False)
        if mode == "grid":
            self._reflow()

    # 保存和恢复列宽。
    def _restore_col_widths(self) -> None:
        """从配置恢复各列宽度，无保存值时采用默认比例。"""
        widths = Config.instance().get("ui.game_list_col_widths", None)
        if not isinstance(widths, list) or len(widths) != 3:
            return
        self._restoring_col_widths = True
        try:
            for i, w in enumerate(widths):
                try:
                    self._table.setColumnWidth(i, max(60, int(w)))
                except (TypeError, ValueError):
                    pass
        finally:
            self._restoring_col_widths = False

    def _on_col_resized(self, _index: int, _old: int, _new: int) -> None:
        """保存用户调整后的三列宽度，跳过程序恢复引发的尺寸事件。"""
        if getattr(self, "_restoring_col_widths", False):
            return
        widths = [self._table.columnWidth(i) for i in range(3)]
        if all(w > 0 for w in widths):
            Config.instance().set("ui.game_list_col_widths", widths)

    def _apply_filter(self) -> None:
        """按游戏名称和英文名称过滤卡片，空查询显示全部卡片。"""
        q = self._search.text().strip().lower()
        for c in self._cards:
            g = self._svc.get_game(c.game_id)
            visible = (not q) or bool(g and (q in g.name.lower() or q in g.name_en.lower()))
            c.setVisible(visible)

    def _on_scale(self, val: int) -> None:
        """立即应用卡片缩放并重排网格，延后持久化滑块值以减少连续写盘。"""
        self._scale = val / 100.0
        cw = int(self.GRID_BASE_W * self._scale)
        ch = int(self.GRID_BASE_H * self._scale)
        for c in self._cards:
            c.resize_card(cw, ch)
        self._reflow()
        self._scale_persist.start()  # 防抖后持久化缩放值

    def _persist_scale(self) -> None:
        """防抖后持久化游戏卡片缩放百分比。"""
        Config.instance().set("ui.game_card_scale", int(round(self._scale * 100)))

    # 拖放排序。
    def _reorder_drop(self, drag_id: str, pos) -> None:
        """按拖放位置重排并保存游戏顺序，搜索过滤期间拒绝排序以避免位置歧义。"""
        if self._search.text().strip():
            return  # 搜索过滤时不排序，避免错位
        if drag_id not in self._display_ids:
            return
        target = len(self._display_ids)
        # 卡片周围半个网格间距也计入拖动目标，卡片间隙归属最近项；所有卡片之外视为末尾。
        pad = self._grid_container.grid.spacing() // 2
        for i, cid in enumerate(self._display_ids):
            card = next((c for c in self._cards if c.game_id == cid), None)
            if not card or not card.isVisible():
                continue
            if card.geometry().adjusted(-pad, -pad, pad, pad).contains(pos):
                target = i
                break
        cur = self._display_ids.index(drag_id)
        ids = list(self._display_ids)
        ids.pop(cur)
        if target > cur:
            target -= 1
        ids.insert(min(max(target, 0), len(ids)), drag_id)
        self._svc.reorder_games(ids)
        self.reload()
        self.games_changed.emit()
