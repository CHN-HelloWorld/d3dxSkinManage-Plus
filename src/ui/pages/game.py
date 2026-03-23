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

from PySide6.QtCore import QPoint, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QDragEnterEvent, QDragMoveEvent, QDropEvent, QPainter
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
