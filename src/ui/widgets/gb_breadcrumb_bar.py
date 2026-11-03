"""GameBanana 多级分类导航。

首段显示游戏名称，其余段支持子分类菜单。点击段名返回该层级并发出路径变化信号，
打开菜单后由上层获取子分类并通过 set_children 返回数据。
分类选择器使用可搜索的滚动列表，限制菜单尺寸。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
    QWidgetAction,
)

from src.services.gamebanana.models import GbCategory
from src.ui import icons, theme
from src.ui.widgets.spinner import LoadingIndicator
from src.ui.win_effects import acrylic_surface_alpha
from src.utils.i18n import localize_category_name
from src.utils.i18n import t as tr
from src.utils.qt_timers import call_later


class _ScrollableCategoryPicker(QWidget):
    """嵌入菜单的子分类选择器，包含搜索框与可滚动列表。"""

    picked = Signal(object)  # GbCategory
    retry_requested = Signal()

    # 加载页和内容页共用固定尺寸区域，避免 QMenu 按首次尺寸布局后无法展示完整列表。
    LIST_AREA_HEIGHT = 300  # 固定列表与加载区域的高度，内容超出时在内部滚动。

    def __init__(self, parent: QWidget | None = None) -> None:
        """创建分类筛选输入、固定高度列表和加载错误视图，并连接选择及重试信号。"""
        super().__init__(parent)
        from PySide6.QtWidgets import QStackedWidget

        outer = QVBoxLayout(self)
        outer.setContentsMargins(4, 4, 4, 4)
        outer.setSpacing(4)
        self._search = QLineEdit()
        self._search.setPlaceholderText(tr("筛选…"))
        self._search.setClearButtonEnabled(True)
        self._search.textChanged.connect(self._on_filter)
        outer.addWidget(self._search)
        # 列表页与加载页共用固定尺寸，保持菜单位置稳定。
        self._stack = QStackedWidget()
        self._stack.setFixedHeight(self.LIST_AREA_HEIGHT)
        self._stack.setMinimumWidth(240)
        self._list = QListWidget()
        self._list.itemActivated.connect(self._on_item)
        self._list.itemClicked.connect(self._on_item)
        self._loading = LoadingIndicator(tr("正在加载分类…"))
        self._stack.addWidget(self._list)  # index 0：内容
        self._stack.addWidget(self._loading)  # index 1：加载态
        self._error = QWidget()
        error_layout = QVBoxLayout(self._error)
        error_layout.addStretch()
        self._error_label = QLabel(tr("分类加载失败"))
        self._error_label.setWordWrap(True)
        self._error_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        error_layout.addWidget(self._error_label)
        self._retry = QPushButton()
        self._retry.setObjectName("categoryRetryButton")
        self._retry.setText(tr("重新加载"))
        icons.apply_icon(self._retry, "actions/refresh")
        self._retry.clicked.connect(self.retry_requested.emit)
        error_layout.addWidget(self._retry, alignment=Qt.AlignmentFlag.AlignHCenter)
        error_layout.addStretch()
        self._stack.addWidget(self._error)
        outer.addWidget(self._stack)
        self._all: list[GbCategory] = []

    def set_categories(self, cats: list[GbCategory]) -> None:
        """保存分类并应用当前筛选，异步返回数据时保留用户已输入的查询。"""
        self._all = list(cats)
        self._on_filter(self._search.text())
        self._stack.setCurrentIndex(0)

    def set_loading(self) -> None:
        """切换到与列表同尺寸的加载页，等待分类数据。"""
        self._all = []
        self._list.clear()
        self._retry.setEnabled(False)
        self._stack.setCurrentIndex(1)

    def set_error(self, message: str) -> None:
        """显示分类请求错误并重新启用重试按钮。"""
        self._error_label.setText(message)
        self._retry.setEnabled(True)
        self._stack.setCurrentWidget(self._error)

    def _rebuild_list(self, cats: list[GbCategory]) -> None:
        """按本地化名称和条目数量重建分类列表；空列表显示不可选择的占位项。"""
        self._list.clear()
        if not cats:
            # 无分类或无筛选结果时显示不可选择的占位项。
            empty = QListWidgetItem(tr("（无分类）"))
            empty.setFlags(Qt.ItemFlag.NoItemFlags)
            self._list.addItem(empty)
            return
        for cat in cats:
            display = localize_category_name(cat.name)
            label = f"{display} ({cat.item_count})" if cat.item_count else display
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, cat)
            self._list.addItem(item)

    def _on_filter(self, text: str) -> None:
        """以不区分大小写的子串匹配原始名称和本地化名称，更新可见分类列表。"""
        text = (text or "").strip().lower()
        if not text:
            self._rebuild_list(self._all)
            return
        # 同时匹配原始名称和翻译名称。
        filtered = [c for c in self._all if text in c.name.lower() or text in localize_category_name(c.name).lower()]
        self._rebuild_list(filtered)

    def _on_item(self, item: QListWidgetItem) -> None:
        """从列表项取出分类对象并发出选择信号，忽略无数据的占位项。"""
        cat = item.data(Qt.ItemDataRole.UserRole)
        if cat is not None:
            self.picked.emit(cat)

    def focus_search(self) -> None:
        """菜单显示后同步窗口与输入框焦点，使系统输入法向分类筛选框提交文字。"""
        if not self.isVisible():
            return
        # Windows 默认不激活弹出菜单，仅设置控件焦点会让输入法仍指向主窗口的旧输入框。
        self.window().activateWindow()
        self._search.setFocus(Qt.FocusReason.PopupFocusReason)


class GbBreadcrumbBar(QFrame):
    """面包屑栏。"""

    # 用户点击某段段名 → 截断到该段；参数：截断后的 path
    path_changed = Signal(list)
    # 用户打开某段下拉 → 需要拉子分类；参数：父分类 id（首段以外）
    children_requested = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        """初始化分类路径、子分类缓存以及各路径节点的菜单选择器引用。"""
        super().__init__(parent)
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(8, 4, 8, 4)
        self._layout.setSpacing(4)
        self._path: list[GbCategory] = []
        # parent_id -> list[GbCategory]; 缓存最近一次 set_children 的结果
        self._children_cache: dict[int, list[GbCategory]] = {}
        self._segments: list[QToolButton] = []
        self._separators: list[QLabel] = []
        # 记录打开菜单的段索引，以便异步分类返回后更新对应选择器。
        self._open_picker_index: int | None = None
        self._pickers: dict[int, _ScrollableCategoryPicker] = {}

    # 公开操作接口。
    def set_path(self, path: list[GbCategory]) -> None:
        """保存分类路径副本并重建路径按钮与菜单。"""
        self._path = list(path)
        self._rebuild()

    def set_children(self, parent_id: int, children: list[GbCategory]) -> None:
        """接收 children_requested 对应的子分类结果。"""
        self._children_cache[parent_id] = list(children)
        # 当前选择器属于该父分类时立即更新列表。
        if self._open_picker_index is not None:
            idx = self._open_picker_index
            if 0 <= idx < len(self._path) and self._path[idx].id == parent_id:
                picker = self._pickers.get(idx)
                if picker is not None:
                    picker.set_categories(children)

    def segment_texts(self) -> list[str]:
        """返回当前路径按钮显示的文字列表。"""
        return [b.text() for b in self._segments]

    def set_children_error(self, parent_id: int, message: str) -> None:
        """仅向仍打开且父分类匹配的选择器展示异步请求错误。"""
        idx = self._open_picker_index
        if idx is not None and 0 <= idx < len(self._path) and self._path[idx].id == parent_id:
            picker = self._pickers.get(idx)
            if picker is not None:
                picker.set_error(message)

    def segment_has_dropdown(self, index: int) -> bool:
        """判断指定路径位置是否有效且附有子分类菜单。"""
        if index < 0 or index >= len(self._segments):
            return False
        return self._segments[index].menu() is not None

    def click_segment(self, index: int) -> None:
        """触发指定层级的路径选择。"""
        if 0 <= index < len(self._path):
            self.path_changed.emit(self._path[: index + 1])

    def open_dropdown(self, index: int) -> None:
        """打开指定层级的分类菜单。"""
        if index <= 0 or index >= len(self._path):
            return
        self.children_requested.emit(self._path[index].id)

    @staticmethod
    def _segment_label(cat: GbCategory, index: int) -> str:
        """游戏名称按原文显示，分类名称使用当前语言包翻译。"""
        if index == 0:
            return cat.name
        return localize_category_name(cat.name)

    # 内部布局与菜单管理。
    def _rebuild(self) -> None:
        # 同时清理控件和伸缩布局项，防止重复构建时残留空白间距。
        """移除旧路径控件及选择器，再按当前路径创建分隔符和可展开的子分类按钮。"""
        while self._layout.count():
            item = self._layout.takeAt(0)
            if item is None:
                continue
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
            # 伸缩项移出布局后无需调用 deleteLater。
        self._segments.clear()
        self._separators.clear()
        # 重建时清理旧选择器，下一次打开菜单时重新创建。
        self._pickers.clear()
        self._open_picker_index = None
        for i, cat in enumerate(self._path):
            if i > 0:
                sep = QLabel("/")
                sep.setStyleSheet(f"color:{theme.color('text.tertiary')}; padding:0 4px;")
                self._layout.addWidget(sep)
                self._separators.append(sep)
            btn = QToolButton()
            btn.setAutoRaise(True)
            label = self._segment_label(cat, i)
            if i == 0:
                btn.setText(label)
                # 首段不带菜单
            else:
                btn.setText(label)
                btn.setObjectName("crumbSeg")
                menu = QMenu(btn)
                self._style_category_menu(menu)
                btn.setMenu(menu)
                btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
                menu.aboutToShow.connect(lambda b=i: self._populate_menu(b))
                menu.aboutToHide.connect(lambda b=i: self._on_menu_hide(b))
                btn.clicked.connect(lambda _, b=i: self.path_changed.emit(self._path[: b + 1]))
            self._layout.addWidget(btn)
            self._segments.append(btn)
        self._layout.addStretch(1)

    def _style_category_menu(self, menu: QMenu) -> None:
        """构造半透明分类浮层，内部搜索框和列表保持透明。

        首次显示时应用系统模糊，透明背景属性只设置一次，避免重建原生窗口。
        """
        menu.setObjectName("catPickerMenu")
        menu.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        bg = theme.rgba("bg.card", acrylic_surface_alpha(0.45))
        menu.setStyleSheet(
            f"QMenu#catPickerMenu {{ background-color: {bg};"
            f" border: 1px solid {theme.color('stroke.card')}; border-radius: 8px; padding: 4px; }}"
            # 内部 UI 全透明，仅外层面板着色
            "QMenu#catPickerMenu QWidget,"
            " QMenu#catPickerMenu QLineEdit,"
            " QMenu#catPickerMenu QListWidget { background: transparent; }"
            f"QMenu#catPickerMenu QPushButton#categoryRetryButton {{ background-color: {theme.color('control.bg')};"
            f" border: 1px solid {theme.color('stroke.control')}; border-radius: 4px; padding: 6px 12px; }}"
            "QMenu#catPickerMenu QPushButton#categoryRetryButton:hover {"
            f" background-color: {theme.color('control.bg.hover')}; }}"
            "QMenu#catPickerMenu QPushButton#categoryRetryButton:pressed {"
            f" background-color: {theme.color('bg.pressed')}; }}"
        )

    def _populate_menu(self, index: int) -> None:
        """为路径节点创建可筛选的子分类菜单，优先显示缓存，否则发出加载请求。

        记录当前选择器位置以接收异步结果，并将键盘焦点放入筛选框。
        """
        if index <= 0 or index >= len(self._segments):
            return
        btn = self._segments[index]
        menu = btn.menu()
        menu.clear()
        parent_id = self._path[index].id
        # 将可滚动选择器嵌入菜单，避免大量子分类使菜单超出屏幕。
        picker = _ScrollableCategoryPicker(menu)
        action = QWidgetAction(menu)
        action.setDefaultWidget(picker)
        menu.addAction(action)
        picker.picked.connect(lambda cat, i=index: self._on_picker_picked(i, cat, menu))
        picker.retry_requested.connect(lambda: self._retry_children(parent_id, picker))
        self._pickers[index] = picker
        self._open_picker_index = index
        if parent_id in self._children_cache:
            picker.set_categories(self._children_cache[parent_id])
        else:
            picker.set_loading()  # 先显示加载状态，子分类返回后填充列表。
            self.children_requested.emit(parent_id)
        call_later(0, picker, picker.focus_search)

    def _retry_children(self, parent_id: int, picker: _ScrollableCategoryPicker) -> None:
        """将选择器恢复为加载状态并重新请求指定父分类的子项。"""
        picker.set_loading()
        self.children_requested.emit(parent_id)

    def _on_menu_hide(self, index: int) -> None:
        """菜单关闭时清除对应的活动选择器位置。"""
        if self._open_picker_index == index:
            self._open_picker_index = None

    def _on_picker_picked(self, index: int, cat: GbCategory, menu: QMenu) -> None:
        """关闭菜单，将选中分类追加到对应祖先路径并发出路径变化信号。"""
        menu.hide()
        new_path = self._path[: index + 1] + [cat]
        self.path_changed.emit(new_path)
