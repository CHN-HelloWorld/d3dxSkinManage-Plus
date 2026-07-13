"""类别总览的路径面包屑。

子控件使用手动几何，在已分配宽度内排列，避免内容宽度通过最小尺寸约束挤压导航栏。
空间不足时从左侧整段折叠到溢出菜单，优先保留当前节点。
分隔符使用按显示尺寸渲染并随主题着色的 SVG。
"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import QLabel, QMenu, QPushButton, QToolButton, QWidget

from src.ui import acrylic, icons, theme


class BreadcrumbBar(QWidget):
    """自管理面包屑地址栏：SVG 分隔符 + 从左折叠溢出，最小宽度趋近 0 不挤邻居。"""

    # 用户点击某段 → 导航到该节点；参数：目标 parent_id（根为空串 ""）
    navigate = Signal(str)

    _HEIGHT = 32
    _SEP_PX = 14  # chevron 分隔符绘制尺寸（正方）
    _SEP_CELL = 18  # 分隔符占位宽度（含左右各 2px 呼吸）
    _MIN_W = 56  # 最小宽度保留溢出入口及当前路径提示，避免压缩导航区。

    def __init__(self, parent: QWidget | None = None) -> None:
        """创建定高分类路径栏，并为宽度不足时隐藏的祖先节点准备溢出菜单。"""
        super().__init__(parent)
        self.setFixedHeight(self._HEIGHT)
        # 节点数据：[(id, name)]，index 0 恒为根节点（id=""）。最后一个 = 当前层级。
        self._nodes: list[tuple[str, str]] = [("", "")]
        self._node_btns: list[QPushButton] = []
        self._seps: list[QLabel] = []  # chevron 分隔符对象池
        self._root_label = ""

        # 溢出按钮（被折叠的左侧父级都进它的下拉菜单）
        self._overflow = QToolButton(self)
        self._overflow.setText("···")
        self._overflow.setCursor(Qt.CursorShape.PointingHandCursor)
        self._overflow.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self._overflow.hide()
        self._overflow_menu = QMenu(self._overflow)
        # 首次 show 前就准备透明原生窗口；Show 后再改属性会重建 HWND 并丢掉刚施加的磨砂。
        acrylic.prepare_acrylic_menu(self._overflow_menu)
        self._overflow.setMenu(self._overflow_menu)

        self._restyle()

    # 对外 API

    def set_items(self, root_label: str, path: list[tuple[str, str]]) -> None:
        """设置面包屑内容。

        root_label: 根节点显示名（如「全部分类」）。
        path: 从根**之下**到当前层级的 [(id, name), ...]；空列表表示当前就在根。
        """
        self._root_label = root_label
        self._nodes = [("", root_label)] + list(path)
        self._rebuild_buttons()
        self._relayout()

    def retheme(self) -> None:
        """主题切换后重着色（分隔符 SVG 换色 + 按钮样式重涂）。"""
        self._restyle()
        acrylic.prepare_acrylic_menu(self._overflow_menu)
        self._relayout()

    # 内部：样式

    def _btn_style_link(self) -> str:
        """返回可导航路径节点的主题文字和悬停背景样式。"""
        return (
            f"QPushButton {{ color: {theme.color('accent')}; font-size: 12px;"
            f" border: none; padding: 2px 6px; background: transparent; }}"
            f"QPushButton:hover {{ background-color: {theme.rgba('accent', 0.12)};"
            f" border-radius: 3px; }}"
        )

    def _btn_style_current(self) -> str:
        """返回当前路径节点的加粗非链接样式。"""
        return (
            f"QPushButton {{ color: {theme.color('text.secondary')}; font-size: 12px;"
            f" font-weight: bold; border: none; padding: 2px 6px; background: transparent; }}"
        )

    def _restyle(self) -> None:
        """更新溢出按钮样式及路径分隔图标的主题颜色。"""
        self._overflow.setStyleSheet(
            f"QToolButton {{ color: {theme.color('text.secondary')}; font-size: 14px;"
            f" font-weight: bold; border: none; padding: 0 4px; background: transparent; }}"
            f"QToolButton:hover {{ background-color: {theme.rgba('accent', 0.12)};"
            f" border-radius: 3px; }}"
            "QToolButton::menu-indicator { image: none; width: 0; }"
        )
        # 分隔符 chevron 随主题换色（重建对象池时按当前色生成）
        self._chevron = icons.pixmap("actions/chevron-right", self._SEP_PX, "text.tertiary")

    # 内部：构建按钮

    def _rebuild_buttons(self) -> None:
        """销毁旧节点按钮，按路径重建祖先导航链接和不可点击的当前节点。"""
        for b in self._node_btns:
            b.deleteLater()
        self._node_btns.clear()
        n = len(self._nodes)
        for i, (cid, cname) in enumerate(self._nodes):
            btn = QPushButton(cname or self._root_label, self)
            btn.setFlat(True)
            is_current = i == n - 1
            if is_current:
                btn.setStyleSheet(self._btn_style_current())
                btn.setCursor(Qt.CursorShape.ArrowCursor)
            else:
                btn.setStyleSheet(self._btn_style_link())
                btn.setCursor(Qt.CursorShape.PointingHandCursor)
                btn.clicked.connect(lambda _=False, pid=cid: self.navigate.emit(pid))
            self._node_btns.append(btn)

    def _sep(self, i: int) -> QLabel:
        """从对象池获取指定分隔符，不足时创建。"""
        while i >= len(self._seps):
            lbl = QLabel(self)
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lbl.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            self._seps.append(lbl)
        lbl = self._seps[i]
        lbl.setPixmap(self._chevron)
        return lbl

    # 内部：折叠布局（从左折叠、保右）

    def _relayout(self) -> None:
        """根据可用宽度优先保留路径末端，将容纳不下的祖先节点收进溢出菜单。

        复用分隔控件并隐藏多余实例，避免尺寸变化时重建整个控件树。
        """
        n = len(self._node_btns)
        if n == 0:
            return
        avail = max(0, self.width())
        widths = [b.sizeHint().width() for b in self._node_btns]
        SEP = self._SEP_CELL
        ovf_w = self._overflow.sizeHint().width()

        # 先试全部平铺（无溢出）：Σ段宽 + (n-1) 个分隔符
        total_full = sum(widths) + SEP * (n - 1)
        if total_full <= avail:
            visible = list(range(n))
            show_ovf = False
        else:
            # 预留溢出按钮宽度，优先保留当前层级及其相邻路径。
            show_ovf = True
            used = ovf_w
            visible = []
            for i in range(n - 1, -1, -1):
                add = widths[i] + SEP  # 每个可见段前都有一个分隔符（跟在溢出/前段后）
                if not visible or used + add <= avail:
                    visible.insert(0, i)
                    used += add
                else:
                    break

        hidden = [i for i in range(n) if i not in visible]
        if not hidden:
            show_ovf = False

        # 逐个定位：[溢出?] › 段 › 段 …（相邻单元之间插 chevron，首单元前不插）
        x = 0
        if show_ovf:
            self._overflow.setGeometry(x, 0, ovf_w, self._HEIGHT)
            self._overflow.show()
            x += ovf_w
            self._rebuild_overflow_menu(hidden)
        else:
            self._overflow.hide()

        sep_i = 0
        first_cell = not show_ovf  # 有溢出时溢出是首单元，其后每段都要分隔符
        for idx in visible:
            if not first_cell:
                sep = self._sep(sep_i)
                sep.setGeometry(x, 0, SEP, self._HEIGHT)
                sep.show()
                sep_i += 1
                x += SEP
            first_cell = False
            b = self._node_btns[idx]
            w = widths[idx]
            b.setGeometry(x, 0, w, self._HEIGHT)
            b.show()
            x += w

        # 隐藏未用到的分隔符与被折叠的段按钮
        for j in range(sep_i, len(self._seps)):
            self._seps[j].hide()
        for i in hidden:
            self._node_btns[i].hide()

    def _rebuild_overflow_menu(self, hidden: list[int]) -> None:
        """将隐藏节点转换为菜单项，并把各自节点标识绑定到导航信号。"""
        self._overflow_menu.clear()
        for i in hidden:
            cid, cname = self._nodes[i]
            act = self._overflow_menu.addAction(cname or self._root_label)
            act.triggered.connect(lambda _=False, pid=cid: self.navigate.emit(pid))

    # Qt 覆写

    def sizeHint(self) -> QSize:
        """返回路径栏的建议宽度与固定高度。"""
        return QSize(max(self._MIN_W, 200), self._HEIGHT)

    def minimumSizeHint(self) -> QSize:
        # 限制最小宽度，避免长路径压缩左侧导航空间。
        """返回路径栏允许压缩到的最小宽度与固定高度。"""
        return QSize(self._MIN_W, self._HEIGHT)

    def resizeEvent(self, e) -> None:
        """在可用宽度变化后重新分配路径节点与溢出菜单。"""
        super().resizeEvent(e)
        self._relayout()
