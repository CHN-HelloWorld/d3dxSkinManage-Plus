"""关于页面，展示社区致敬、版本时间线、项目团队和使用许可。

文本按实际宽度计算换行高度，标题使用自绘像素字体；项目蓝色与致敬金色
由局部调色板提供，卡片表面仍遵循全局主题。悬停效果通过属性动画绘制。
时间线支持横向拖动和惯性滚动，以连接线及节点关联里程碑，并显示滚动进度。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import (
    Property,
    QAbstractAnimation,
    QEasingCurve,
    QPoint,
    QPointF,
    QPropertyAnimation,
    QRectF,
    QSize,
    Qt,
    QUrl,
)
from PySide6.QtGui import (
    QBrush,
    QColor,
    QDesktopServices,
    QFont,
    QFontMetrics,
    QIcon,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QRadialGradient,
)
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QGraphicsEffect,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QScroller,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from src.ui import anim, icons, theme
from src.ui.smooth_scroll import SmoothScroller
from src.ui.widgets.project_story_card import ProjectStoryCard
from src.utils.i18n import N_
from src.utils.i18n import t as tr
from src.utils.paths import get_resources_dir
from src.utils.qt_timers import call_later

# 版本历史、团队及许可展示数据。


_TIMELINE = [
    (N_("2022-10-04"), N_("试做"), "3DMiModsManage Type-A", N_("第一个试做版本，仅作测试存在过，代码已删除。"), True),
    ("2022-10-06", N_("首发"), "3DMiModsManage Type-B", N_("第一个在社区发布过的版本（取代码文件创建时间）。"), True),
    ("2022-11-16", N_("迭代"), "3DMiModsManage Type-C", N_("接续 Type-B 的更新版本（取代码文件创建时间）。"), True),
    (
        "2023-04-21",
        N_("升级"),
        "3DMiM2T",
        N_(
            "3DMiModsManage 升级版，界面已接近 d3dxSkinManage、控件布局更优；仅 Demo，"
            "未能正常工作（取代码文件创建时间）。"
        ),
        True,
    ),
    (
        "2023-05-15",
        N_("正式"),
        N_("d3dxSkinManage 正式发布"),
        N_("代码创建于 05-15，2023-05-22 正式发布，d3dxSkinManage 自此成型。"),
        True,
    ),
    ("2026-03-06", N_("收官"), N_("d3dxSkinManage 末版"), N_("d3dxSkinManage 维护的最后一个版本。"), True),
    (
        "2026-03-09",
        N_("传承"),
        N_("d3dxSkinManage-Plus 立项"),
        N_("全新独立项目，以现代技术栈与设计重新出发，承接社区需求（与原项目无代码关联）。"),
        False,
    ),
    ("2026-08-29", N_("内测"), N_("第一次内测"), N_("d3dxSkinManage-Plus 开启第一次内测。"), False),
    (
        "2026-11-01",
        N_("正式"),
        N_("d3dxSkinManage-Plus 正式发布"),
        N_("d3dxSkinManage-Plus 正式面向社区发布。"),
        False,
    ),
]

# (头像文件名, 昵称, 角色名, 角色描述, GitHub URL（无主页时为 None）, 是否创始人)
_TEAM = [
    (
        "aether.png",
        "Aether",
        N_("创始人"),
        N_("项目发起人 · 产品 / 架构 · 核心开发"),
        "https://github.com/CHN-HelloWorld",
        True,
    ),
    ("numlinka.png", "numlinka", N_("项目协作者"), N_("特别支持"), "https://github.com/numlinka", False),
    ("kkevin0001.png", "hunxuankai", N_("贡献者"), N_("辅助开发 · 测试"), "https://github.com/kKevin0001", False),
    ("菊理千姬.jpg", "菊理千姬", N_("特别支持"), N_("持续关注 · 鼎力支持"), None, False),
    ("为人民服务.jpg", "为人民服务", N_("特别支持"), N_("持续关注 · 鼎力支持"), None, False),
    ("昀熙.jpg", "昀熙", N_("内测贡献"), N_("有效问题反馈 · 建议获采纳"), None, False),
    ("自求多芙吧!.jpg", "自求多芙吧!", N_("内测贡献"), N_("有效问题反馈 · 建议获采纳"), None, False),
]

_LICENSE_ALLOW = [
    N_("自由查看、学习、修改本项目源代码"),
    N_("个人与社区的非商业用途下自由使用、复制、分发"),
    N_("在保留完整署名与许可声明的前提下二次开发并向社区贡献"),
]
_LICENSE_DENY = [
    N_("出售、倒卖本程序，或借其直接牟利等任何商业用途"),
    N_("删除、篡改本「关于」页中的原作者致敬与贡献者署名"),
    N_("冒充作者、歪曲事实或以任何方式误导他人对作者/贡献者身份的认知"),
]
_LICENSE_NOTE = [
    N_("Mod 资源版权归各自创作者所有，本工具仅作管理"),
    N_("软件按「现状」提供，不对因使用产生的任何后果担责"),
]

# 背景径向渐变参数：中心比例、横纵半径、颜色、中心透明度及透明停靠点。
# 蓝金强调层始终显示，中心景深层仅用于不透明背景。
_GLOWS = {
    "dark": [
        (0.78, -0.08, 900, 520, (96, 205, 255), 0.16, 0.60),
        (0.08, 1.02, 760, 480, (242, 200, 75), 0.09, 0.58),
        (0.50, 0.40, 1200, 700, (40, 48, 64), 0.40, 0.70),
    ],
    "light": [
        (0.78, -0.08, 900, 520, (0, 103, 192), 0.10, 0.60),
        (0.08, 1.02, 760, 480, (176, 125, 0), 0.07, 0.58),
        (0.50, 0.40, 1200, 700, (220, 228, 240), 0.50, 0.70),
    ],
}


# 颜色 / 字体 / 头像工具


def _gold() -> str:
    """金色致敬色（主题调色板无此 token，按明暗本地推导）。"""
    return "#f2c84b" if theme.current_theme() == "dark" else "#b07d00"


def _accent() -> str:
    """返回关于页的项目蓝色，按明暗主题选择，不跟随系统强调色。"""
    return "#60cdff" if theme.current_theme() == "dark" else "#0067c0"


def _rgba_hex(hex_color: str, alpha: float) -> str:
    """将六位十六进制颜色与透明度组合为样式表可用的 RGBA 文本。"""
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"rgba({r}, {g}, {b}, {alpha})"


def _qc(hex_color: str, alpha: float = 1.0) -> QColor:
    """十六进制 + alpha → QColor（用于 hover 颜色插值的端点）。"""
    c = QColor(hex_color)
    c.setAlphaF(alpha)
    return c


def _lerp_color(c0: QColor, c1: QColor, t: float) -> QColor:
    """在两 QColor 间按 t(0..1) 线性插值（含 alpha）。"""
    return QColor(
        round(c0.red() + (c1.red() - c0.red()) * t),
        round(c0.green() + (c1.green() - c0.green()) * t),
        round(c0.blue() + (c1.blue() - c0.blue()) * t),
        round(c0.alpha() + (c1.alpha() - c0.alpha()) * t),
    )


def _qss_rgba(c: QColor) -> str:
    """将 Qt 颜色转换为样式表 RGBA 表达式，透明度保留三位小数。"""
    return f"rgba({c.red()}, {c.green()}, {c.blue()}, {c.alphaF():.3f})"


def _font(px: float, weight: QFont.Weight = QFont.Weight.Normal) -> QFont:
    """为自绘文本创建像素字号字体，小数字号取整。

    普通控件可能被全局样式覆盖，须通过 _font_css 设置其局部字号。
    """
    f = QFont()
    f.setPixelSize(int(px + 0.5))
    f.setWeight(weight)
    return f


# QFont.Weight → CSS font-weight（写进内联样式用）。
_WEIGHT_CSS = {
    QFont.Weight.Normal: 400,
    QFont.Weight.Medium: 500,
    QFont.Weight.DemiBold: 600,
    QFont.Weight.Bold: 700,
    QFont.Weight.ExtraBold: 800,
}


def _font_css(px: float, weight: QFont.Weight = QFont.Weight.Normal) -> str:
    """生成控件内联样式中的字号和字重。

    全局 QSS 会覆盖 QFont 的字号，需将 font-size 写入控件自身样式表，
    使布局测量和实际绘制使用一致的尺寸。
    """
    return f"font-size: {int(px + 0.5)}px; font-weight: {_WEIGHT_CSS.get(weight, 400)};"


class _WrapLabel(QLabel):
    """根据可用宽度调整最小高度，完整显示换行文本。

    部分布局约束下 QLabel 的高度提示不会自动应用，因此尺寸变化时重新计算 heightForWidth。
    """

    def __init__(self, text: str = "") -> None:
        """创建随可用宽度自动换行的说明标签。"""
        super().__init__(text)
        self.setWordWrap(True)

    def resizeEvent(self, event) -> None:
        """宽度变化后按换行文本的实际高度更新最小高度，避免布局裁切说明。"""
        super().resizeEvent(event)
        w = self.width()
        if w > 0:
            h = self.heightForWidth(w)
            if h > 0 and h != self.minimumHeight():
                self.setMinimumHeight(h)


def _label(
    text: str,
    px: float,
    token: str,
    *,
    weight: QFont.Weight = QFont.Weight.Normal,
    wrap: bool = False,
    max_w: int | None = None,
    align: Qt.AlignmentFlag | None = None,
) -> QLabel:
    """按关于页字体和颜色规则创建标签，可配置换行、最大宽度及对齐方式。"""
    lbl: QLabel = _WrapLabel(text) if wrap else QLabel(text)
    lbl.setFont(_font(px, weight))
    col = _accent() if token == "accent" else theme.color(token)
    lbl.setStyleSheet(f"color: {col}; background: transparent; {_font_css(px, weight)}")
    if wrap:
        lbl.setWordWrap(True)
    if max_w is not None:
        lbl.setMaximumWidth(max_w)
    if align is not None:
        lbl.setAlignment(align)
    return lbl


def _avatars_dir() -> Path:
    """返回随应用资源提供的关于页头像目录。"""
    return get_resources_dir() / "about" / "avatars"


def _circular_avatar(filename: str, size: int, ring: str) -> QPixmap:
    """加载头像并裁成圆形（带一圈描边）。文件缺失时返回带描边的空底圆。"""
    src = QPixmap(str(_avatars_dir() / filename))
    out = QPixmap(size, size)
    out.fill(Qt.GlobalColor.transparent)
    p = QPainter(out)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    path = QPainterPath()
    inset = 2.0
    path.addEllipse(inset, inset, size - 2 * inset, size - 2 * inset)
    p.setClipPath(path)
    if not src.isNull():
        scaled = src.scaled(
            size,
            size,
            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation,
        )
        x = (scaled.width() - size) // 2
        y = (scaled.height() - size) // 2
        p.drawPixmap(-x, -y, scaled)
    else:
        p.fillRect(0, 0, size, size, QColor(theme.color("bg.hover")))
    p.setClipping(False)
    pen = QPen(QColor(ring))
    pen.setWidthF(2.0)
    p.setPen(pen)
    p.drawEllipse(int(inset), int(inset), size - 2 * int(inset), size - 2 * int(inset))
    p.end()
    return out


# 自定义控件


class _HoverCard(QFrame):
    """支持悬停描边、上浮、渐变和外部悬停回调的卡片。

    属性动画同时驱动描边和位移；描边在 paintEvent 中插值，避免逐帧更新样式
    触发布局并覆盖位移。进入时结合当前位移还原布局基准，保持快速移入移出和
    窗口缩放后的动画连续。静态卡片不启用悬停效果。
    """

    def __init__(
        self,
        *,
        radius: int = 12,
        left_accent: str | None = None,
        hover_border: str | None = None,
        lift: bool = False,
        tint: str | None = None,
    ) -> None:
        """保存圆角、描边、渐变和上浮策略，初始化悬停进度及布局基线。"""
        super().__init__()
        self.setObjectName("hoverCard")
        self._radius = radius
        self._left = left_accent
        self._hover_border = hover_border
        self._lift = lift
        self._tint = tint
        self._on_hover = None
        self._home_y: int | None = None
        self._t = 0.0
        self._t_anim = None
        self._reapplying = False  # moveEvent 重施上浮时防递归
        self._interactive = bool(hover_border) or lift
        # 描边插值端点：静止 stroke.card → hover 点亮色（无 hover_border 则恒为 stroke.card）。
        self._border0 = QColor(theme.color("stroke.card"))
        self._border1 = QColor(hover_border) if hover_border else QColor(self._border0)
        self._restyle()

    def set_hover_callback(self, cb) -> None:
        """登记接收鼠标进入和离开状态的回调。"""
        self._on_hover = cb

    def _restyle(self) -> None:
        """更新卡片静态背景和圆角，交互描边交给绘制事件插值以减少动画期间的布局更新。"""
        bg = theme.surface_color("bg.card", "panel")
        # 致敬横幅始终叠加左侧金色渐变，背景图模式使用透明颜色以保留底层材质。
        if self._tint:
            bg = (
                f"qlineargradient(x1:0, y1:0, x2:1, y2:0,"
                f" stop:0 {_rgba_hex(self._tint, 0.20)},"
                f" stop:0.5 {_rgba_hex(self._tint, 0.05)},"
                f" stop:0.72 {bg}, stop:1 {bg})"
            )
        if self._interactive:
            # 交互描边由 paintEvent 插值，避免逐帧改写样式触发布局并重置上浮位置。
            self.setStyleSheet(
                f"QFrame#hoverCard {{ background: {bg}; border: none; border-radius: {self._radius}px; }}"
            )
        else:
            left = f" border-left: 3px solid {self._left};" if self._left else ""
            self.setStyleSheet(
                f"QFrame#hoverCard {{ background: {bg};"
                f" border: 1px solid {_qss_rgba(self._border0)};"
                f"{left} border-radius: {self._radius}px; }}"
            )

    def paintEvent(self, event) -> None:
        """在样式背景上绘制随悬停进度插值的描边，不改写样式表或布局。"""
        super().paintEvent(event)  # QSS 画底色 + 圆角（静态卡连边框/左金条一起画）
        if not self._interactive:
            return
        # 交互卡：在 QSS 底色之上补一圈插值描边（不动 QSS，不触发布局）。
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        pen = QPen(_lerp_color(self._border0, self._border1, self._t))
        pen.setWidthF(1.0)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        r = float(self._radius)
        p.drawRoundedRect(QRectF(0.5, 0.5, self.width() - 1.0, self.height() - 1.0), r, r)
        p.end()

    def _get_t(self) -> float:
        """返回供 Qt 属性动画读取的悬停过渡进度。"""
        return self._t

    def _lift_offset(self) -> int:
        """按当前悬停进度计算上浮像素数，未启用上浮时返回零。"""
        return round(3 * self._t) if self._lift else 0

    def _apply_lift(self) -> None:
        """把卡片移到「基准位 − 当前上浮量」。带防递归（move 会再触发 moveEvent）。"""
        if self._home_y is None:
            return
        target = self._home_y - self._lift_offset()
        if self.y() != target:
            self._reapplying = True
            try:
                self.move(self.x(), target)
            except RuntimeError:
                pass
            self._reapplying = False

    def _set_t(self, v: float) -> None:
        """限制并应用悬停过渡进度，重绘描边并同步可选上浮位移。"""
        self._t = max(0.0, min(1.0, v))
        self.update()  # 重绘插值描边（仅 paintEvent，不 setStyleSheet、不触发布局）
        if self._lift:
            self._apply_lift()

    def moveEvent(self, event) -> None:
        """父布局调整卡片位置后，立即恢复悬停上浮偏移。

        上浮通过 ``move`` 实现，父布局重排会将卡片恢复到布局位置。悬停状态下将该位置
        作为新基准，并在同一事件内重新应用偏移，避免布局调整与动画交替改变位置。
        """
        super().moveEvent(event)
        if self._reapplying or not self._lift or self._t <= 0.0 or self._home_y is None:
            return
        if self.y() != self._home_y - self._lift_offset():
            self._home_y = self.y()  # 布局给的即未上浮基准
            self._apply_lift()

    hoverT = Property(float, fget=_get_t, fset=_set_t)

    def _animate_t(self, target: float) -> None:
        """停止未结束的悬停过渡再动画至目标进度，禁用动画时直接应用。"""
        if not anim.is_enabled():
            self._set_t(target)
            return
        # 停掉上一条未完成的过渡，避免快速反复进出时两条动画同写 hoverT/位置而抖动。
        if self._t_anim is not None:
            try:
                self._t_anim.stop()
            except RuntimeError:
                pass
        a = QPropertyAnimation(self, b"hoverT", self)
        a.setDuration(anim.DURATION["slow"])
        a.setStartValue(self._t)
        a.setEndValue(target)
        a.setEasingCurve(QEasingCurve.Type.InOutCubic)
        a.start(QAbstractAnimation.DeletionPolicy.DeleteWhenStopped)
        self._t_anim = a

    def enterEvent(self, event) -> None:
        """进入卡片时按需记录上浮基线、启动悬停过渡，并通知回调。"""
        if self._interactive:
            if self._lift:
                # 由「当前位置 + 已上浮量」反推布局真实基准：静止时即布局位，半途进出时还原不跳变。
                self._home_y = self.y() + round(3 * self._t)
            self._animate_t(1.0)
        if self._on_hover:
            self._on_hover(True)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        """离开卡片时启动恢复过渡，并通知悬停回调。"""
        if self._interactive:
            self._animate_t(0.0)
        if self._on_hover:
            self._on_hover(False)
        super().leaveEvent(event)


class _FadeButton(QPushButton):
    """通过悬停属性动画平滑更新链接按钮的背景、描边、文本和图标颜色。"""

    def __init__(
        self,
        text: str,
        url: str,
        *,
        bg0: QColor,
        bg1: QColor,
        br0: QColor,
        br1: QColor,
        fg0: QColor,
        fg1: QColor,
        icon_name: str = "brand/github",
        icon_px: int = 15,
        text_px: float = 13,
    ) -> None:
        """创建外部链接按钮，保存背景、描边与文字的颜色端点及图标配置。"""
        super().__init__(text)
        self._bg0, self._bg1 = bg0, bg1
        self._br0, self._br1 = br0, br1
        self._fg0, self._fg1 = fg0, fg1
        self._icon_name = icon_name
        self._icon_px = icon_px
        self._t = 0.0
        self._t_anim = None
        self._text_px = text_px
        self.setFont(_font(text_px, QFont.Weight.DemiBold))
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setIconSize(QSize(icon_px, icon_px))
        self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        self.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(url)))
        self._apply()

    def _apply(self) -> None:
        """按当前过渡进度插值按钮背景、描边和文字颜色，并同步图标着色。"""
        t = self._t
        bg = _lerp_color(self._bg0, self._bg1, t)
        br = _lerp_color(self._br0, self._br1, t)
        fg = _lerp_color(self._fg0, self._fg1, t)
        self.setStyleSheet(
            f"QPushButton {{ color: {_qss_rgba(fg)}; background: {_qss_rgba(bg)};"
            f" border: 1px solid {_qss_rgba(br)}; border-radius: 13px;"
            f" padding: 5px 14px; text-align: left; {_font_css(self._text_px, QFont.Weight.DemiBold)} }}"
        )
        self.setIcon(QIcon(icons.colored_pixmap_from(self._icon_name, self._icon_px, fg.name())))

    def _get_t(self) -> float:
        """返回链接按钮当前的悬停颜色过渡进度。"""
        return self._t

    def _set_t(self, v: float) -> None:
        """保存属性动画传入的过渡进度，并重新应用按钮颜色。"""
        self._t = v
        self._apply()

    hoverT = Property(float, fget=_get_t, fset=_set_t)

    def _animate_to(self, target: float) -> None:
        """将按钮悬停颜色过渡到目标进度，禁用动画时直接设置。"""
        if not anim.is_enabled():
            self._set_t(target)
            return
        a = QPropertyAnimation(self, b"hoverT", self)
        a.setDuration(anim.DURATION["slow"])
        a.setStartValue(self._t)
        a.setEndValue(target)
        a.setEasingCurve(QEasingCurve.Type.OutCubic)
        a.start(QAbstractAnimation.DeletionPolicy.DeleteWhenStopped)
        self._t_anim = a

    def enterEvent(self, event) -> None:
        """鼠标进入链接按钮时过渡到悬停颜色。"""
        self._animate_to(1.0)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        """鼠标离开链接按钮时恢复静止颜色。"""
        self._animate_to(0.0)
        super().leaveEvent(event)


class _GlowDot(QWidget):
    """徽标中的强调色辉光点，通过多段渐变绘制中心和柔和外缘。"""

    def __init__(self, color: str, d: int = 7) -> None:
        """为发光状态点保存颜色和直径，并让鼠标事件穿透到所在控件。"""
        super().__init__()
        self._c = QColor(color)
        self._d = d
        self.setFixedSize(d + 14, d + 14)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

    def paintEvent(self, _event) -> None:
        """绘制径向渐变光晕及中心实色圆点。"""
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        c = QPointF(self.width() / 2, self.height() / 2)
        r = self.width() / 2.0
        glow = QRadialGradient(c, r)
        g0 = QColor(self._c)
        g0.setAlphaF(0.95)
        gm = QColor(self._c)
        gm.setAlphaF(0.45)
        g1 = QColor(self._c)
        g1.setAlphaF(0.0)
        glow.setColorAt(0.0, g0)
        glow.setColorAt(0.42, gm)
        glow.setColorAt(1.0, g1)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(glow))
        p.drawEllipse(c, r, r)
        p.setBrush(self._c)
        p.drawEllipse(c, self._d / 2.0, self._d / 2.0)
        p.end()


class _Connector(QFrame):
    """连接里程碑卡片与时间线中轴的竖线。"""

    def __init__(self, color: str) -> None:
        """创建固定尺寸的竖向连接线，并使鼠标事件穿透到所在卡片。"""
        super().__init__()
        self.setFixedSize(2, 16)
        self.setStyleSheet(f"background: {color}; border: none;")
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)


class _AxisNode(QWidget):
    """时间线中轴分段，包含水平线和节点；关联卡片悬停时放大节点。"""

    HEIGHT = 40

    def __init__(self, node_color: str, line_color: str) -> None:
        """保存时间线节点和轴线颜色，初始化节点缩放状态与固定高度。"""
        super().__init__()
        self._node = QColor(node_color)
        self._line = QColor(line_color)
        self._scale = 1.0
        self._scale_anim = None
        self.setFixedHeight(self.HEIGHT)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

    def _get_scale(self) -> float:
        """返回 Qt 属性动画读取的时间线节点缩放比例。"""
        return self._scale

    def _set_scale(self, v: float) -> None:
        """更新节点缩放比例并请求重绘。"""
        self._scale = v
        self.update()

    nodeScale = Property(float, fget=_get_scale, fset=_set_scale)

    def set_hovered(self, on: bool) -> None:
        """按卡片悬停状态缩放对应时间线节点，禁用动画时直接应用。"""
        target = 1.25 if on else 1.0
        if not anim.is_enabled():
            self._set_scale(target)
            return
        a = QPropertyAnimation(self, b"nodeScale", self)
        a.setDuration(anim.DURATION["normal"])
        a.setStartValue(self._scale)
        a.setEndValue(target)
        a.setEasingCurve(QEasingCurve.Type.OutCubic)
        a.start(QAbstractAnimation.DeletionPolicy.DeleteWhenStopped)
        self._scale_anim = a

    def paintEvent(self, _event) -> None:
        """绘制时间轴、节点光晕和空心圆环，中心底色遮住贯穿节点的轴线。"""
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        cy = self.height() / 2
        cx = self.width() / 2
        c = QPointF(cx, cy)
        pen = QPen(self._line)
        pen.setWidth(2)
        p.setPen(pen)
        p.drawLine(0, int(cy), self.width(), int(cy))
        s = self._scale
        p.setPen(Qt.PenStyle.NoPen)
        soft = QColor(self._node)
        soft.setAlphaF(0.18)
        p.setBrush(soft)
        p.drawEllipse(c, 11 * s, 11 * s)
        # 先绘制强调色外圆，再以背景色覆盖中心，形成空心节点并遮住下方中轴线。
        p.setBrush(self._node)
        p.drawEllipse(c, 8 * s, 8 * s)
        p.setBrush(QColor(theme.color("bg.base")))
        p.drawEllipse(c, 5 * s, 5 * s)
        p.end()


# 两端留白与淡出带等宽，保证滚动到边界时首尾卡片完全可见。
_TIMELINE_EDGE_FADE = 56


class _EdgeFadeEffect(QGraphicsEffect):
    """对时间线左右边缘应用透明度渐变。

    遮罩附加到滚动内容，通过滚动量将视口边缘映射到内容坐标。
    仅在两条窄带内使用 DestinationOut 清除透明度，中间内容保持原样。
    """

    def __init__(self, viewport: QWidget, scrollbar, fade: int = _TIMELINE_EDGE_FADE) -> None:
        """将边缘渐隐效果绑定到视口生命周期，并保存水平滚动条与渐隐宽度。"""
        # 绑定视口生命周期，避免 PySide6 回收临时特效对象后移除渐隐遮罩。
        super().__init__(viewport)
        self._viewport = viewport
        self._sb = scrollbar
        self._fade = float(fade)

    def draw(self, painter: QPainter) -> None:
        """按当前视口在轨道图像中的位置擦除两侧透明渐变，再绘制带渐隐边缘的内容。"""
        offset = QPoint()
        pixmap = self.sourcePixmap(
            Qt.CoordinateSystem.LogicalCoordinates,
            offset,
            QGraphicsEffect.PixmapPadMode.NoPad,
        )
        if pixmap.isNull():
            return
        vw = self._viewport.width()
        fade = self._fade
        if vw > 0 and fade > 0.0:
            dpr = pixmap.devicePixelRatio() or 1.0
            ph = pixmap.height() / dpr
            ox = float(offset.x()) if offset is not None else 0.0
            # 视口可见窗口换算到 pixmap（=track）逻辑坐标：左沿 = 横向滚动量 − 源偏移。
            left = float(self._sb.value()) - ox
            right = left + vw
            qp = QPainter(pixmap)
            qp.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationOut)
            # 左带：外缘全擦(α255) → 内缘不擦(α0)。
            gl = QLinearGradient(left, 0.0, left + fade, 0.0)
            gl.setColorAt(0.0, QColor(0, 0, 0, 255))
            gl.setColorAt(1.0, QColor(0, 0, 0, 0))
            qp.fillRect(QRectF(left, 0.0, fade, ph), QBrush(gl))
            # 右带：内缘不擦(α0) → 外缘全擦(α255)。
            gr = QLinearGradient(right - fade, 0.0, right, 0.0)
            gr.setColorAt(0.0, QColor(0, 0, 0, 0))
            gr.setColorAt(1.0, QColor(0, 0, 0, 255))
            qp.fillRect(QRectF(right - fade, 0.0, fade, ph), QBrush(gr))
            qp.end()
        painter.drawPixmap(offset if offset is not None else QPointF(0.0, 0.0), pixmap)


class _TimelineProgress(QWidget):
    """随横向滚动位置填充的时间线进度条。"""

    def __init__(self) -> None:
        """创建不截获鼠标的细进度条，初始位置为时间线起点。"""
        super().__init__()
        self._ratio = 0.0
        self.setFixedSize(180, 3)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

    def set_ratio(self, r: float) -> None:
        """限制滚动比例，并仅在可见进度有实际变化时请求重绘。"""
        r = max(0.0, min(1.0, r))
        if abs(r - self._ratio) > 0.001:
            self._ratio = r
            self.update()

    def paintEvent(self, _event) -> None:
        """绘制主题渐变进度条，为时间线起点保留最小可见长度。"""
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        w = self.width()
        h = self.height()
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(theme.color("bg.subtle")))
        p.drawRoundedRect(QRectF(0, 0, w, h), h / 2, h / 2)
        fw = w * (0.08 + self._ratio * 0.92)
        grad = QLinearGradient(0, 0, fw, 0)
        grad.setColorAt(0.0, QColor(_accent()))
        grad.setColorAt(1.0, QColor(_gold()))
        p.setBrush(QBrush(grad))
        p.drawRoundedRect(QRectF(0, 0, fw, h), h / 2, h / 2)
        p.end()


class _HeroTitle(QWidget):
    """在单个控件内绘制普通标题和渐变文字，保持基线对齐。

    可用宽度不足时等比缩小字号，避免横向溢出。
    """

    def __init__(self, plain: str, grad: str, px: int) -> None:
        """按标题两部分的字体度量保存自然宽度与固定高度，为自适应绘制预留空间。"""
        super().__init__()
        self._plain = plain
        self._grad = grad
        self._px = px
        fm = QFontMetrics(_font(px, QFont.Weight.ExtraBold))
        self._nat_w = fm.horizontalAdvance(plain) + fm.horizontalAdvance(grad) + 6
        self._pad = 8
        self._h = fm.height() + self._pad * 2
        self.setFixedHeight(self._h)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)

    def sizeHint(self) -> QSize:
        """返回标题按原定字体绘制时的自然布局尺寸。"""
        return QSize(self._nat_w, self._h)

    def minimumSizeHint(self) -> QSize:
        """提供较小的标题布局下限，文字由绘制阶段按可用宽度缩放。"""
        # 较小的宽度下限允许窄窗口缩放标题，而不由文本自然宽度撑开页面。
        return QSize(120, self._h)

    def resizeEvent(self, event) -> None:
        """可用宽度变化时请求重绘标题，以重新计算字号和居中位置。"""
        super().resizeEvent(event)
        self.update()

    def _scaled_font(self, avail_w: int) -> QFont:
        """标题宽度不足时按比例缩小字号，同时保留最小可读字号。"""
        px = self._px
        if avail_w > 0 and self._nat_w > avail_w:
            px = max(20, int(px * avail_w / self._nat_w))
        return _font(px, QFont.Weight.ExtraBold)

    def paintEvent(self, _event) -> None:
        """按可用宽度居中绘制普通标题和渐变标题，使两部分共享文字基线。"""
        p = QPainter(self)
        p.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.TextAntialiasing, True)
        f = self._scaled_font(self.width())
        p.setFont(f)
        fm = QFontMetrics(f)
        wp = fm.horizontalAdvance(self._plain)
        wg = fm.horizontalAdvance(self._grad)
        x0 = max(0, (self.width() - (wp + wg)) // 2)
        y = (self.height() - fm.height()) // 2 + fm.ascent()
        p.setPen(QColor(theme.color("text.primary")))
        p.drawText(x0, y, self._plain)
        g = QLinearGradient(x0 + wp, 0, x0 + wp + wg, 0)
        g.setColorAt(0.0, QColor(_accent()))
        g.setColorAt(0.5, QColor("#9aa8ff"))
        g.setColorAt(1.0, QColor(_gold()))
        p.setPen(QPen(QBrush(g), 0))
        p.drawText(x0 + wp, y, self._grad)
        p.end()


# 关于页


class AboutPage(QWidget):
    """关于页：产品介绍、项目短片、致敬、团队与许可，致敬区含可拖动时间线。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        """创建纵向滚动的关于页，初始化时间线、渐入目标和按需创建的故事播放器。"""
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        # 页面仅纵向滚动，标题自行适配可用宽度。
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        self._scroll.viewport().setStyleSheet("background: transparent;")
        self._scroll.viewport().setAutoFillBackground(False)
        SmoothScroller.attach(self._scroll)
        root.addWidget(self._scroll)
        self._timeline_area: QScrollArea | None = None
        self._progress: _TimelineProgress | None = None
        self._reveal_targets: list[QWidget] = []
        self._story_player = None
        self._build()

    # 泛光背景
    def paintEvent(self, _event) -> None:
        """绘制固定的页面底色及强调渐变，不随内容滚动。

        不透明模式使用三层渐变，背景图模式使用半透明底层和两层强调渐变。
        """
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = self.rect()
        bg_active = theme.is_background_active()
        if bg_active:
            p.fillRect(rect, _qc(theme.color("bg.layer"), theme.ACRYLIC["nav"]))
        else:
            p.fillRect(rect, QColor(theme.color("bg.base")))
        glows = _GLOWS.get(theme.current_theme(), _GLOWS["dark"])
        for i, glow in enumerate(glows):
            if bg_active and i == 2:
                continue  # 背景图模式跳过中心景深，仅留两抹强调辉光
            self._paint_glow(p, rect, *glow)
        p.end()

    @staticmethod
    def _paint_glow(
        p: QPainter,
        rect,
        cxf: float,
        cyf: float,
        rx: int,
        ry: int,
        rgb: tuple[int, int, int],
        alpha: float,
        stop: float,
    ) -> None:
        """在指定相对位置绘制椭圆径向辉光，结束后恢复调用方的画笔坐标与状态。"""
        if rx <= 0 or ry <= 0:
            return
        cx = rect.width() * cxf
        cy = rect.height() * cyf
        p.save()
        p.translate(cx, cy)
        p.scale(1.0, ry / rx)  # 将圆形径向渐变沿纵向压缩为椭圆辉光。
        grad = QRadialGradient(0.0, 0.0, float(rx))
        center = QColor(rgb[0], rgb[1], rgb[2])
        center.setAlphaF(alpha)
        edge = QColor(rgb[0], rgb[1], rgb[2])
        edge.setAlphaF(0.0)
        grad.setColorAt(0.0, center)
        grad.setColorAt(stop, edge)
        grad.setColorAt(1.0, edge)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(grad))
        p.drawEllipse(QPointF(0.0, 0.0), float(rx), float(rx))
        p.restore()

    # 构建页面。
    def _build(self) -> None:
        """按顺序组装项目介绍、故事、致谢、团队、许可与页脚，并登记可渐入的分区。"""
        self._reveal_targets = []
        content = QWidget()
        content.setStyleSheet("background: transparent;")
        v = QVBoxLayout(content)
        v.setContentsMargins(40, 36, 40, 40)
        v.setSpacing(56)

        for section in (
            self._hero(),
            self._project_story(),
            self._tribute_section(),
            self._team_section(),
            self._license_section(),
        ):
            v.addWidget(section)
            self._reveal_targets.append(section)
        v.addWidget(self._footer())
        v.addStretch(1)

        self._scroll.setWidget(content)

    def _project_story(self) -> QWidget:
        """创建故事卡片及其内联播放器容器，点击时才启动媒体资源。"""
        self._story_section = QWidget()
        self._story_layout = QVBoxLayout(self._story_section)
        self._story_layout.setContentsMargins(0, 0, 0, 0)
        self._story_layout.setSpacing(0)
        self._story_card = ProjectStoryCard()
        self._story_card.clicked.connect(self._open_story)
        self._story_layout.addWidget(self._story_card)
        return self._story_section

    def _open_story(self) -> None:
        """复用现有播放器或重新读取媒体配置后创建内联播放器，转移焦点并开始播放。"""
        if self._story_player is not None:
            self._story_player.setFocus()
            return
        # 静态页面不创建解码器或音频输出，打开前重新读取配置以获取最新媒体。
        from src.ui.widgets.project_story_player import ProjectStoryPlayer

        self._story_card.reload()
        self._story_player = ProjectStoryPlayer(self._story_card.story, self._story_section)
        self._story_player.close_requested.connect(self._close_story)
        self._story_layout.addWidget(self._story_player)
        self._story_player.show()
        self._story_player.setFocus(Qt.FocusReason.OtherFocusReason)
        self._story_card.hide()
        self._story_player.start()
        call_later(0, self, self._reveal_story)

    def _reveal_story(self) -> None:
        """滚动关于页以显示仍存在的内联故事播放器。"""
        if self._story_player is not None:
            self._scroll.ensureWidgetVisible(self._story_player, 0, 20)

    def _close_story(self) -> None:
        """释放媒体资源并恢复故事卡片，先转移播放器焦点再隐藏和延后销毁控件。"""
        player, self._story_player = self._story_player, None
        if player is None:
            return
        focus = QApplication.focusWidget()
        restore_focus = self.isVisible() and focus is not None and (focus is player or player.isAncestorOf(focus))
        player.release_media()
        self._story_card.reload()
        self._story_card.show()
        # 先转移播放器焦点再隐藏，避免导航列表自动选中首项并离开关于页。
        if restore_focus:
            self._story_card.setFocus(Qt.FocusReason.OtherFocusReason)
        self._story_layout.removeWidget(player)
        player.hide()
        player.deleteLater()

    def hideEvent(self, event) -> None:
        """离开关于页时关闭故事播放器，呈现方式切换中的临时隐藏除外。"""
        if self._story_player is not None and not self._story_player.changing_presentation:
            self._close_story()
        super().hideEvent(event)

    def showEvent(self, event) -> None:
        """页面显示时重新读取故事卡片配置，反映最新媒体资源。"""
        self._story_card.reload()
        super().showEvent(event)

    def retheme(self) -> None:
        """主题切换后整页按新调色板重建（内容静态、成本低）。"""
        self._close_story()
        self._build()
        self.update()

    def play_intro(self) -> None:
        """进场动画：各区块交错淡入 + 时间线拖动提示回弹。"""
        anim.stagger_reveal(list(self._reveal_targets), step=70)
        call_later(140, self, self._timeline_bounce)

    def _timeline_bounce(self) -> None:
        """时间线可水平滚动且动画已启用时短暂往返移动，提示可滚动方向。"""
        area = self._timeline_area
        if area is None or not anim.is_enabled():
            return
        bar = area.horizontalScrollBar()
        if bar.maximum() <= 0:
            return
        nudge = min(64, bar.maximum())
        a = QPropertyAnimation(bar, b"value", self)
        a.setDuration(720)
        a.setKeyValueAt(0.0, 0)
        a.setKeyValueAt(0.5, nudge)
        a.setKeyValueAt(1.0, 0)
        a.setEasingCurve(QEasingCurve.Type.InOutCubic)
        a.start(QAbstractAnimation.DeletionPolicy.DeleteWhenStopped)
        self._bounce_anim = a  # 保活

    # 通用
    def _kicker(self, text: str, gold: bool = False) -> QWidget:
        """创建由短线和强调色文字组成的分区引导标题。"""
        col = _gold() if gold else _accent()
        w = QWidget()
        w.setStyleSheet("background: transparent;")
        lay = QHBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        line = QFrame()
        line.setFixedSize(22, 2)
        line.setStyleSheet(f"background: {col}; border: none; border-radius: 1px;")
        lay.addWidget(line)
        lbl = _label(text.upper(), 12.5, "accent", weight=QFont.Weight.Bold)
        lbl.setStyleSheet(f"color: {col}; background: transparent; {_font_css(12.5, QFont.Weight.Bold)}")
        lf = lbl.font()
        lf.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.5)
        lbl.setFont(lf)
        lay.addWidget(lbl)
        lay.addStretch(1)
        return w

    def _section_head(self, kicker: str, title: str, sub: str, gold: bool = False) -> QWidget:
        """组合分区引导标题、主标题和可换行的说明文字。"""
        w = QWidget()
        w.setStyleSheet("background: transparent;")
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        lay.addWidget(self._kicker(kicker, gold))
        lay.addWidget(_label(title, 26, "text.primary", weight=QFont.Weight.Bold))
        lay.addWidget(_label(sub, 14.5, "text.tertiary", wrap=True, max_w=720))
        return w

    def _pill(self, text: str, gold: bool = False) -> QLabel:
        """创建按文本宽度收缩的强调色标签，可选择金色配色。"""
        col = _gold() if gold else _accent()
        lbl = QLabel(text)
        lbl.setFont(_font(11, QFont.Weight.Bold))
        lbl.setStyleSheet(
            f"color: {col}; background: {_rgba_hex(col, 0.14)};"
            f" padding: 2px 8px; border-radius: 5px; {_font_css(11, QFont.Weight.Bold)}"
        )
        lbl.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        return lbl

    def _author_link(self, url: str) -> _FadeButton:
        """作者链接按钮，悬停时平滑过渡背景和文字颜色。"""
        g = _gold()
        return _FadeButton(
            "github.com/numlinka",
            url,
            bg0=_qc(g, 0.14),
            bg1=_qc(g, 1.0),
            br0=_qc(g, 0.35),
            br1=_qc(g, 1.0),
            fg0=_qc(theme.color("text.primary"), 1.0),
            fg1=_qc("#2a1e00", 1.0),
        )

    def _person_link(self, url: str) -> _FadeButton:
        """贡献者链接按钮，悬停时同步过渡描边、文字和图标颜色。"""
        accent = _accent()
        sub = theme.color("bg.subtle")
        return _FadeButton(
            "GitHub",
            url,
            bg0=_qc(sub, 1.0),
            bg1=_qc(sub, 1.0),
            br0=_qc(theme.color("stroke.control"), 1.0),
            br1=_qc(accent, 1.0),
            fg0=_qc(theme.color("text.secondary"), 1.0),
            fg1=_qc(accent, 1.0),
            text_px=12,
        )

    # 页面主介绍。
    def _hero(self) -> QWidget:
        """构建项目徽标、可缩放标题、简介与版本入口。"""
        w = QWidget()
        w.setStyleSheet("background: transparent;")
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 8, 0, 0)
        lay.setSpacing(16)

        # 由辉光点和文字组成的徽标。
        accent = _accent()
        badge = QFrame()
        badge.setObjectName("heroBadge")
        badge.setStyleSheet(
            f"QFrame#heroBadge {{ background: {_rgba_hex(accent, 0.14)};"
            f" border: 1px solid {_rgba_hex(accent, 0.22)}; border-radius: 13px; }}"
        )
        badge.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        bl = QHBoxLayout(badge)
        bl.setContentsMargins(12, 4, 15, 4)
        bl.setSpacing(6)
        bl.addWidget(_GlowDot(accent), alignment=Qt.AlignmentFlag.AlignVCenter)
        bl.addWidget(_label(tr("关于本项目"), 13, "accent", weight=QFont.Weight.DemiBold))
        lay.addWidget(badge, alignment=Qt.AlignmentFlag.AlignHCenter)

        title = _HeroTitle("d3dxSkinManage-", "Plus", 52)
        lay.addWidget(title)  # 铺满宽度、自绘居中（窄窗自适应缩放，不撑宽页面）

        lead = _label(
            tr("一款面向社区的现代化 Mod 管理工具 —— 站在前人对社区贡献的肩膀上，以全新的代码与设计重新出发。"),
            17,
            "text.secondary",
            wrap=True,
            max_w=620,
            align=Qt.AlignmentFlag.AlignHCenter,
        )
        lead.setMinimumWidth(520)  # 居中换行标签给足宽度，避免被压窄成多行（高度由 _WrapLabel 保证不裁）
        lay.addWidget(lead, alignment=Qt.AlignmentFlag.AlignHCenter)

        lay.addWidget(self._hero_version(), alignment=Qt.AlignmentFlag.AlignHCenter)
        return w

    def _hero_version(self) -> QWidget:
        """显示项目版本状态和创建日期。"""
        w = QWidget()
        w.setStyleSheet("background: transparent;")
        lay = QHBoxLayout(w)
        lay.setContentsMargins(0, 6, 0, 0)
        lay.setSpacing(10)
        lay.addWidget(_label(tr("当前状态"), 13.5, "text.tertiary"))
        code = QLabel(tr("已发布"))
        code.setFont(_font(13))
        code.setStyleSheet(
            f"color: {theme.color('text.secondary')}; background: {theme.color('bg.subtle')};"
            f" border: 1px solid {theme.color('stroke.control')};"
            f" border-radius: 6px; padding: 3px 10px; {_font_css(13)}"
        )
        lay.addWidget(code)
        lay.addWidget(_label("·", 13.5, "text.tertiary"))
        lay.addWidget(_label(tr("立项 2026-03-09"), 13.5, "text.tertiary"))
        return w

    # 致谢内容。
    def _tribute_section(self) -> QWidget:
        """组合社区贡献致谢、作者信息和时间线展示区域。"""
        w = QWidget()
        w.setStyleSheet("background: transparent;")
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(20)
        lay.addWidget(
            self._section_head(
                tr("Tribute · 致敬"),
                tr("向原 d3dxSkinManage 作者致敬"),
                tr(
                    "每一个繁荣的社区背后，都有一位最早点亮火种的人。本项目与原 "
                    "d3dxSkinManage 没有任何代码或工程上的关联，但那份对社区的开创性贡献，"
                    "值得被郑重记住。"
                ),
                gold=True,
            )
        )
        lay.addWidget(self._tribute_banner())
        lay.addWidget(self._author_card())
        lay.addWidget(self._timeline())
        return w

    def _tribute_banner(self) -> QFrame:
        """创建带金色渐变的致谢横幅，并展示项目独立性说明。"""
        gold = _gold()
        f = _HoverCard(left_accent=gold, tint=gold)
        outer = QHBoxLayout(f)
        outer.setContentsMargins(26, 22, 26, 22)
        outer.setSpacing(18)

        # 使用 SVG 火苗图标保持外观一致，并配以金色圆角背景。
        seal = QLabel()
        seal.setPixmap(icons.colored_pixmap_from("meta/spark", 28, gold))
        seal.setFixedSize(56, 56)
        seal.setAlignment(Qt.AlignmentFlag.AlignCenter)
        seal.setStyleSheet(
            f"background: {_rgba_hex(gold, 0.12)}; border: 1px solid {_rgba_hex(gold, 0.30)}; border-radius: 14px;"
        )
        outer.addWidget(seal, alignment=Qt.AlignmentFlag.AlignTop)

        col = QVBoxLayout()
        col.setSpacing(10)
        col.addWidget(_label(tr("开创者的火种"), 19, "text.primary", weight=QFont.Weight.Bold))
        col.addWidget(
            _label(
                tr(
                    "原 d3dxSkinManage 作者以一己之力，为社区构建了最初的 Mod 管理范式，"
                    "降低了无数创作者与玩家的Mod管理门槛，"
                    "让「换装/改模」这件事第一次变得触手可及。这种从 0 到 1 的开创，"
                    "是后来所有同类工具共同的精神起点。"
                ),
                14.5,
                "text.secondary",
                wrap=True,
            )
        )
        disclaim = _label(
            tr(
                "※ 声明：d3dxSkinManage-Plus 为独立项目，代码完全重写，与原 d3dxSkinManage "
                "项目无任何引用、衍生或隶属关系。此处致谢仅为表彰其对社区的历史贡献。"
            ),
            12.5,
            "text.disabled",
            wrap=True,
        )
        disclaim.setStyleSheet(
            f"color: {theme.color('text.disabled')}; background: transparent;"
            f" padding-top: 10px; border-top: 1px dashed {theme.color('stroke.control')};"
            f" {_font_css(12.5)}"
        )
        col.addWidget(disclaim)
        outer.addLayout(col, 1)
        return f

    def _author_card(self) -> QFrame:
        """创建社区原项目作者的头像、身份说明和外部主页链接卡片。"""
        gold = _gold()
        f = _HoverCard(left_accent=gold)
        lay = QHBoxLayout(f)
        lay.setContentsMargins(24, 18, 24, 18)
        lay.setSpacing(18)

        avatar = QLabel()
        avatar.setPixmap(_circular_avatar("numlinka.png", 72, gold))
        avatar.setFixedSize(72, 72)
        avatar.setStyleSheet("background: transparent;")
        lay.addWidget(avatar)

        info = QVBoxLayout()
        info.setSpacing(6)
        name_row = QHBoxLayout()
        name_row.setSpacing(10)
        name_row.addWidget(_label("numLinka", 20, "text.primary", weight=QFont.Weight.Bold))
        name_row.addWidget(self._pill(tr("原 d3dxSkinManage 作者"), gold=True))
        name_row.addStretch(1)
        info.addLayout(name_row)
        info.addWidget(_label(tr("d3dxSkinManage 系列的开创者与长期维护者（2022 – 2026）"), 13.5, "text.tertiary"))
        links = QHBoxLayout()
        links.setContentsMargins(0, 6, 0, 0)
        links.addWidget(self._author_link("https://github.com/numlinka"))
        links.addStretch(1)
        info.addLayout(links)
        lay.addLayout(info, 1)
        return f

    # 项目时间轴。
    def _milestone_card(self, tag: str, title: str, desc: str, gold: bool) -> _HoverCard:
        """构建可悬停上浮的里程碑卡片，并在进入布局前按文本宽度预估换行高度。"""
        tag, title, desc = tr(tag), tr(title), tr(desc)
        col = _gold() if gold else _accent()
        f = _HoverCard(radius=10, hover_border=col, lift=True)
        f.setFixedWidth(214)
        lay = QVBoxLayout(f)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(6)

        tag_lbl = QLabel(tag)
        tag_lbl.setFont(_font(11, QFont.Weight.Bold))
        tag_lbl.setStyleSheet(
            f"color: {col}; background: {_rgba_hex(col, 0.14)};"
            f" padding: 1px 7px; border-radius: 5px; {_font_css(11, QFont.Weight.Bold)}"
        )
        tag_lbl.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        lay.addWidget(tag_lbl)

        title_lbl = _label(title, 15, "text.primary", weight=QFont.Weight.DemiBold, wrap=True)
        desc_lbl = _label(desc, 12.5, "text.tertiary", wrap=True)
        # 加入布局前按可用文本宽度确定换行高度，避免首次尺寸提示低估卡片高度。
        # 显示后再根据真实宽度校准。
        inner_w = 214 - 14 - 14 - 4
        for _lbl in (title_lbl, desc_lbl):
            _hh = _lbl.heightForWidth(inner_w)
            if _hh > 0:
                _lbl.setMinimumHeight(_hh)
        lay.addWidget(title_lbl)
        lay.addWidget(desc_lbl)
        return f

    def _timeline(self) -> QWidget:
        """统一里程碑卡片槽位高度，组合可拖动时间线、联动节点、边缘渐隐和滚动进度。"""
        # 统一计算卡片及连接线所需槽位高度，使各列节点纵向对齐。
        cards = [self._milestone_card(tag, title, desc, gold) for (_y, tag, title, desc, gold) in _TIMELINE]
        for c in cards:
            c.adjustSize()
        # 顶部余量包含连接线及悬停上浮距离，避免卡片边框被槽位或视口裁切。
        slot_h = max(c.sizeHint().height() for c in cards) + 30

        track = QWidget()
        track.setStyleSheet("background: transparent;")
        h = QHBoxLayout(track)
        # 首尾留白与淡出带等宽，使边界卡片避开透明度遮罩。
        h.setContentsMargins(_TIMELINE_EDGE_FADE, 0, _TIMELINE_EDGE_FADE, 0)
        h.setSpacing(0)
        for i, (year, _tag, _title, _desc, gold) in enumerate(_TIMELINE):
            col = _gold() if gold else _accent()
            node = _AxisNode(col, theme.color("stroke.control"))
            cards[i].set_hover_callback(node.set_hovered)  # 悬浮卡片 → 对应节点放大
            h.addWidget(self._milestone_column(i, year, gold, cards[i], slot_h, node))
        track.adjustSize()

        area = QScrollArea()
        area.setWidget(track)
        area.setWidgetResizable(False)
        area.setFrameShape(QFrame.Shape.NoFrame)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        area.setFixedHeight(track.sizeHint().height() + 6)
        area.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        area.viewport().setStyleSheet("background: transparent;")
        area.viewport().setCursor(Qt.CursorShape.OpenHandCursor)
        QScroller.grabGesture(area.viewport(), QScroller.ScrollerGestureType.LeftMouseButtonGesture)
        self._timeline_area = area

        # 进度条：随横向滚动填充
        progress = _TimelineProgress()
        self._progress = progress
        sb = area.horizontalScrollBar()

        # 遮罩附加到滚动内容，并随滚动量换算视口两端的位置。
        track.setGraphicsEffect(_EdgeFadeEffect(area.viewport(), sb))

        def _upd(*_args) -> None:
            """按水平滚动位置更新进度条，并重绘依赖视口位置的渐隐区域。"""
            m = sb.maximum()
            progress.set_ratio(sb.value() / m if m > 0 else 0.0)
            track.update()  # 滚动 / 视口变宽（rangeChanged）→ 重算淡出带位置

        sb.valueChanged.connect(_upd)
        sb.rangeChanged.connect(_upd)
        call_later(0, track, _upd)

        wrap = QWidget()
        wrap.setStyleSheet("background: transparent;")
        v = QVBoxLayout(wrap)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(8)
        v.addWidget(area)
        v.addWidget(progress, alignment=Qt.AlignmentFlag.AlignHCenter)
        return wrap

    def _milestone_column(
        self, idx: int, year: str, gold: bool, card: QWidget, slot_h: int, node: _AxisNode
    ) -> QWidget:
        """按上下交替布局显示时间轴卡片，并连接到中轴节点。"""
        up = idx % 2 == 0
        col = _gold() if gold else _accent()
        conn = theme.color("stroke.control")
        w = QWidget()
        w.setFixedWidth(244)
        w.setStyleSheet("background: transparent;")
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        # 上槽：up 列放「卡片 + 向下连接线」（底对齐贴近中轴）；down 列留空占位保节点对齐。
        top = QWidget()
        top.setFixedHeight(slot_h)
        top.setStyleSheet("background: transparent;")
        tl = QVBoxLayout(top)
        tl.setContentsMargins(0, 0, 0, 0)
        tl.setSpacing(0)
        if up:
            tl.addStretch(1)
            tl.addWidget(card, alignment=Qt.AlignmentFlag.AlignHCenter)
            tl.addWidget(_Connector(conn), alignment=Qt.AlignmentFlag.AlignHCenter)
        v.addWidget(top)

        v.addWidget(node)
        year_lbl = _label(tr(year), 13, "accent", weight=QFont.Weight.Bold, align=Qt.AlignmentFlag.AlignHCenter)
        year_lbl.setStyleSheet(f"color: {col}; background: transparent; {_font_css(13, QFont.Weight.Bold)}")
        v.addWidget(year_lbl)

        # 下槽：down 列放「向上连接线 + 卡片」（顶对齐贴近中轴/年份）。
        bottom = QWidget()
        bottom.setFixedHeight(slot_h)
        bottom.setStyleSheet("background: transparent;")
        bl = QVBoxLayout(bottom)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(0)
        if not up:
            bl.addWidget(_Connector(conn), alignment=Qt.AlignmentFlag.AlignHCenter)
            bl.addWidget(card, alignment=Qt.AlignmentFlag.AlignHCenter)
            bl.addStretch(1)
        v.addWidget(bottom)
        return w

    # 团队信息。
    def _team_section(self) -> QWidget:
        """按三列网格展示团队成员，并追加贡献者占位卡，顶部预留悬停上浮空间。"""
        w = QWidget()
        w.setStyleSheet("background: transparent;")
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(20)
        lay.addWidget(
            self._section_head(
                tr("Team · 团队"),
                tr("创始人与贡献者"),
                tr("让这个项目从想法变成现实的人。随着社区成长，这份名单会越来越长 —— 欢迎每一位贡献者的加入。"),
            )
        )

        grid = QGridLayout()
        # 顶部保留六像素余量，容纳贡献者卡片的悬停上浮。
        grid.setContentsMargins(0, 6, 0, 0)
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(16)
        cards = [self._person_card(*p) for p in _TEAM]
        cards.append(self._placeholder_person())
        for i, c in enumerate(cards):
            grid.addWidget(c, i // 3, i % 3)
        host = QWidget()
        host.setStyleSheet("background: transparent;")
        host.setLayout(grid)
        lay.addWidget(host)
        return w

    def _person_card(self, filename: str, name: str, role: str, desc: str, url: str | None, founder: bool) -> QFrame:
        """组合成员头像、角色、贡献说明和可选主页入口，用头像环色区分创始人。"""
        role, desc = tr(role), tr(desc)
        ring = _gold() if founder else _accent()
        f = _HoverCard(hover_border=_accent(), lift=True)
        lay = QHBoxLayout(f)
        lay.setContentsMargins(20, 18, 20, 18)
        lay.setSpacing(16)

        avatar = QLabel()
        avatar.setPixmap(_circular_avatar(filename, 60, ring))
        avatar.setFixedSize(60, 60)
        avatar.setStyleSheet("background: transparent;")
        lay.addWidget(avatar)

        info = QVBoxLayout()
        info.setSpacing(3)
        name_row = QHBoxLayout()
        name_row.setSpacing(8)
        name_row.addWidget(_label(name, 17, "text.primary", weight=QFont.Weight.DemiBold))
        name_row.addWidget(self._pill(role, gold=founder))
        name_row.addStretch(1)
        info.addLayout(name_row)
        info.addWidget(_label(desc, 13, "text.tertiary", wrap=True))
        if url:
            link_row = QHBoxLayout()
            link_row.setContentsMargins(0, 6, 0, 0)
            link_row.addWidget(self._person_link(url))
            link_row.addStretch(1)
            info.addLayout(link_row)
        lay.addLayout(info, 1)
        return f

    def _placeholder_person(self) -> QFrame:
        """创建与成员卡片布局一致的贡献邀请占位卡。"""
        # 贡献者占位卡沿用可叠加背景的面板表面色。
        f = QFrame()
        f.setObjectName("personPlaceholder")
        bg = theme.surface_color("bg.card", "panel")
        f.setStyleSheet(
            f"QFrame#personPlaceholder {{ background: {bg};"
            f" border: 1px dashed {theme.color('stroke.control')}; border-radius: 12px; }}"
        )
        lay = QHBoxLayout(f)
        lay.setContentsMargins(20, 18, 20, 18)
        lay.setSpacing(16)

        dot = QLabel("＋")
        dot.setFont(_font(24))
        dot.setFixedSize(60, 60)
        dot.setAlignment(Qt.AlignmentFlag.AlignCenter)
        dot.setStyleSheet(
            f"color: {theme.color('text.tertiary')}; background: {theme.color('bg.hover')};"
            f" border-radius: 30px; {_font_css(24)}"
        )
        lay.addWidget(dot)

        info = QVBoxLayout()
        info.setSpacing(3)
        info.addWidget(_label(tr("虚位以待"), 17, "text.tertiary", weight=QFont.Weight.DemiBold))
        info.addWidget(_label(tr("期待你的贡献"), 13, "text.disabled"))
        info.addStretch(1)
        lay.addLayout(info, 1)
        return f

    # 许可信息。
    def _license_section(self) -> QWidget:
        """将许可正文说明与关键授权信息并排组织为声明分区。"""
        w = QWidget()
        w.setStyleSheet("background: transparent;")
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(20)
        lay.addWidget(
            self._section_head(
                tr("License · 声明"), tr("使用许可与开源声明"), tr("关于本项目的授权方式、使用边界与法律声明。")
            )
        )

        row = QHBoxLayout()
        row.setSpacing(16)
        row.addWidget(self._license_card(), 3)
        row.addWidget(self._license_meta(), 2)
        host = QWidget()
        host.setStyleSheet("background: transparent;")
        host.setLayout(row)
        lay.addWidget(host)
        return w

    def _license_card(self) -> QFrame:
        """展示许可名称、使用边界和允许、禁止及附注条目，正文以仓库许可文件为准。"""
        f = _HoverCard()
        lay = QVBoxLayout(f)
        lay.setContentsMargins(26, 24, 26, 24)
        lay.setSpacing(10)
        lay.addWidget(
            _label(
                tr("PolyForm Noncommercial 1.0.0 + 署名完整性附加条款"),
                18,
                "text.primary",
                weight=QFont.Weight.DemiBold,
                wrap=True,
            )
        )
        lay.addWidget(
            _label(
                tr(
                    "本项目采用 PolyForm Noncommercial License "
                    "1.0.0（非商业·源代码可用许可）并附加署名完整性条款：源代码对社区完全开"
                    "放、可自由学习与改进，但禁止商业牟利，且禁止篡改署名事实。最终以仓库 "
                    "LICENSE 文件为准。"
                ),
                14,
                "text.secondary",
                wrap=True,
            )
        )
        lay.addWidget(self._license_list(tr("✓ 允许"), _LICENSE_ALLOW, "✓"))
        lay.addWidget(self._license_list(tr("✕ 禁止"), _LICENSE_DENY, "✕"))
        lay.addWidget(self._license_list(tr("附注"), _LICENSE_NOTE, "·"))
        return f

    def _license_list(self, head: str, items: list[str], mark: str) -> QWidget:
        """为许可条目添加对应标记、语义颜色和分隔线，并允许长说明换行。"""
        w = QWidget()
        w.setStyleSheet("background: transparent;")
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 6, 0, 0)
        v.setSpacing(5)
        head_lbl = _label(head, 12, "text.tertiary", weight=QFont.Weight.Bold)
        v.addWidget(head_lbl)
        mark_col = theme.color("danger") if mark == "✕" else _accent() if mark == "✓" else theme.color("text.tertiary")
        for i, it in enumerate(items):
            line = QWidget()
            line.setStyleSheet("background: transparent;")
            hl = QHBoxLayout(line)
            hl.setContentsMargins(0, 0, 0, 0)
            hl.setSpacing(8)
            m = _label(mark, 13.5, "accent", weight=QFont.Weight.Bold)
            m.setStyleSheet(f"color: {mark_col}; background: transparent; {_font_css(13.5, QFont.Weight.Bold)}")
            m.setFixedWidth(14)
            hl.addWidget(m, alignment=Qt.AlignmentFlag.AlignTop)
            hl.addWidget(_label(tr(it), 13.5, "text.secondary", wrap=True), 1)
            v.addWidget(line)
            if i < len(items) - 1:  # 绘制条目间分隔线。
                div = QFrame()
                div.setFixedHeight(1)
                div.setStyleSheet(f"background: {theme.color('stroke.divider')}; border: none;")
                v.addWidget(div)
        return w

    def _license_meta(self) -> QWidget:
        """创建许可类型、项目时间与独立性关系等关键资料行。"""
        w = QWidget()
        w.setStyleSheet("background: transparent;")
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(10)
        rows = [
            (tr("许可证"), tr("PolyForm NC 1.0.0 +附加"), None),
            (tr("商业牟利"), tr("禁止"), theme.color("danger")),
            (tr("篡改署名"), tr("禁止"), theme.color("danger")),
            (tr("立项时间"), "2026-03-09", None),
            (tr("发布时间"), "2026-11-01", None),
            (tr("与原 d3dxSkinManage 关系"), tr("独立 · 无引用"), None),
        ]
        for k, val, vcol in rows:
            v.addWidget(self._meta_row(k, val, vcol))
        v.addStretch(1)
        return w

    def _meta_row(self, key: str, val: str, vcol: str | None) -> QFrame:
        """构建键值对形式的资料行，可单独强调值的颜色。"""
        f = QFrame()
        f.setObjectName("metaRow")
        f.setStyleSheet(
            f"QFrame#metaRow {{ background: {theme.color('bg.subtle')};"
            f" border: 1px solid {theme.color('stroke.control')}; border-radius: 8px; }}"
        )
        lay = QHBoxLayout(f)
        lay.setContentsMargins(14, 11, 14, 11)
        lay.addWidget(_label(key, 13, "text.tertiary"))
        lay.addStretch(1)
        val_lbl = _label(val, 13.5, "text.primary", weight=QFont.Weight.DemiBold)
        if vcol:
            val_lbl.setStyleSheet(f"color: {vcol}; background: transparent; {_font_css(13.5, QFont.Weight.DemiBold)}")
        lay.addWidget(val_lbl)
        return f

    # 页脚
    def _footer(self) -> QWidget:
        """构建关于页底部分隔线、社区致谢和版权说明。"""
        w = QWidget()
        w.setStyleSheet("background: transparent;")
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 24, 0, 8)
        v.setSpacing(8)
        line = QFrame()
        line.setFixedHeight(1)
        line.setStyleSheet(f"background: {theme.color('stroke.divider')}; border: none;")
        v.addWidget(line)
        v.addSpacing(16)

        gold = _gold()
        l1 = QLabel(f'Made with <span style="color:{gold}">&#9829;</span> for the community · d3dxSkinManage-Plus')
        l1.setTextFormat(Qt.TextFormat.RichText)
        l1.setFont(_font(13))
        l1.setStyleSheet(f"color: {theme.color('text.disabled')}; background: transparent; {_font_css(13)}")
        l1.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        v.addWidget(l1, alignment=Qt.AlignmentFlag.AlignHCenter)

        l2 = _label(
            tr("© 2026 · 致敬每一位为社区点亮火种的人"), 13, "text.disabled", align=Qt.AlignmentFlag.AlignHCenter
        )
        v.addWidget(l2, alignment=Qt.AlignmentFlag.AlignHCenter)
        return w
