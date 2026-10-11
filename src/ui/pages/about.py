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
        N_("全新独立项目，以现代技术栈与设计重新出发，承接社区需求（与原项目无代码关联）。正式发布时间待定。"),
        False,
    ),
    ("2026-08-29", N_("内测"), N_("第一次内测"), N_("d3dxSkinManage-Plus 开启第一次内测。"), False),
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
