"""分类预设界面的主题颜色。"""

from PySide6.QtGui import QColor

from src.ui import icons, theme


def _tint(token, amount=0.10):
    """按给定前景比例将主题色混入卡片底色，返回不透明十六进制颜色。"""
    front, back = QColor(theme.color(token)), QColor(theme.color("bg.card"))
    return QColor(
        *(
            round(a * amount + b * (1 - amount))
            for a, b in zip((front.red(), front.green(), front.blue()), (back.red(), back.green(), back.blue()))
        )
    ).name()


def stylesheet():
    """根据当前主题生成分类预设窗口样式，并生成复选标记所需的主题 SVG 路径。"""
    c = theme.color
    check = (icons.write_themed_svg("meta/check", "text.on_accent") or "").replace("\\", "/")
    return f"""
    QDialog#categoryPresetDialog {{ background: transparent; color: {c("text.primary")}; }}
    #categoryPresetDialog QWidget {{ background: transparent; }}
    #categoryPresetDialog QLabel {{ background: transparent; color: {c("text.primary")}; font-size: 13px; }}
    #categoryPresetDialog QLabel[presetRole="title"] {{ font-size: 22px; font-weight: 600; }}
    #categoryPresetDialog QLabel[presetRole="subtitle"] {{ font-size: 17px; font-weight: 600; }}
    #categoryPresetDialog QLabel[presetRole="strong"] {{ font-weight: 600; }}
    #categoryPresetDialog QLabel[presetRole="muted"] {{ color: {c("text.secondary")}; font-size: 12px; }}
    #categoryPresetDialog QLabel[presetRole="stat"] {{ font-size: 26px; font-weight: 600; }}
    #categoryPresetDialog QLabel[presetRole="image"] {{ background: {c("bg.subtle")};
        border: 1px solid {c("stroke.card")}; border-radius: 8px; color: {c("text.tertiary")}; }}
    #categoryPresetDialog QLabel[presetRole="notice"] {{ color: {c("text.secondary")}; }}
    #categoryPresetDialog QFrame[presetRole="panel"] {{ background: {c("bg.card")};
        border: 1px solid {c("stroke.card")}; border-radius: 9px; }}
    #categoryPresetDialog QFrame[presetRole="drop"] {{ background: {c("bg.card")};
        border: 1px dashed {c("stroke.control")}; border-radius: 10px; }}
    #categoryPresetDialog QFrame[presetRole="footer"] {{ background: {c("bg.subtle")};
        border: none; border-top: 1px solid {c("stroke.card")}; }}
    #categoryPresetDialog QFrame[presetRole="notice"] {{ background: {_tint("accent")};
        border: 1px solid {c("stroke.card")}; border-radius: 7px; }}
    #categoryPresetDialog QFrame[presetTone="danger"] {{ background: {_tint("danger")}; }}
    #categoryPresetDialog QFrame[presetTone="warning"] {{ background: {_tint("warning")}; }}
    #categoryPresetDialog QFrame[presetTone="success"] {{ background: {_tint("success")}; }}
    #categoryPresetDialog QPushButton {{ background: {c("control.bg")}; color: {c("text.primary")};
        border: 1px solid {c("stroke.control")}; border-radius: 5px; padding: 4px 14px; font-size: 13px; }}
    #categoryPresetDialog QPushButton:hover {{ background: {c("bg.hover")}; }}
    #categoryPresetDialog QPushButton:pressed {{ background: {c("bg.pressed")}; }}
    #categoryPresetDialog QPushButton:focus {{ border: 1px solid {c("accent")}; }}
    #categoryPresetDialog QPushButton[presetPrimary="true"] {{ background: {c("accent")};
        color: {c("text.on_accent")}; border: 1px solid {c("accent")}; font-weight: 600; }}
    #categoryPresetDialog QPushButton[presetPrimary="true"]:hover {{ background: {c("accent.hover")}; }}
    #categoryPresetDialog QPushButton[presetQuiet="true"] {{ color: {c("accent")};
        background: transparent; border: 1px solid transparent; padding: 4px 7px; }}
    #categoryPresetDialog QPushButton[presetQuiet="true"]:hover {{ background: {_tint("accent")}; }}
    #categoryPresetDialog QPushButton:disabled {{ color: {c("text.disabled")};
        background: {c("control.bg.disabled")}; border-color: {c("stroke.card")}; }}
    #categoryPresetDialog QPushButton[presetRole="operation"] {{ text-align: left;
        font-size: 16px; padding: 18px; min-height: 100px; border-radius: 9px; }}
    #categoryPresetDialog QLineEdit {{ min-height: 30px; padding: 2px 9px; border-radius: 5px;
        background: {c("control.bg")}; color: {c("text.primary")}; border: 1px solid {c("stroke.control")}; }}
    #categoryPresetDialog QLineEdit:focus {{ border-bottom: 2px solid {c("accent")}; }}
    #categoryPresetDialog QLineEdit:read-only {{ color: {c("text.secondary")}; background: {c("bg.subtle")}; }}
    #categoryPresetDialog QLineEdit[presetInvalid="true"] {{ border: 1px solid {c("danger")}; }}
    #categoryPresetDialog QCheckBox {{ color: {c("text.primary")}; font-size: 13px; spacing: 8px; }}
    #categoryPresetDialog QCheckBox::indicator:checked {{ image: url("{check}"); }}
    #categoryPresetDialog QTreeWidget {{ background: {c("bg.card")}; border: none; color: {c("text.primary")};
        font-size: 12px; outline: 0; selection-background-color: {_tint("accent", 0.14)};
        selection-color: {c("text.primary")}; }}
    #categoryPresetDialog QTreeWidget::item {{ padding: 5px 3px; border-bottom: 1px solid {c("stroke.divider")}; }}
    #categoryPresetDialog QTreeWidget::item:hover {{ background: {c("bg.hover")}; }}
    #categoryPresetDialog QTreeWidget::item:selected {{ background: {_tint("accent", 0.14)}; }}
    #categoryPresetDialog QHeaderView::section {{ background: {c("bg.subtle")}; color: {c("text.secondary")};
        border: none; border-bottom: 1px solid {c("stroke.card")}; padding: 8px 6px; font-size: 11px; }}
    #categoryPresetDialog QScrollArea {{ border: none; background: transparent; }}
    #categoryPresetDialog QSplitter::handle {{ background: transparent; width: 12px; }}
    #categoryPresetDialog QProgressBar {{ border: none; border-radius: 4px; background: {c("bg.pressed")};
        min-height: 8px; max-height: 8px; }}
    #categoryPresetDialog QProgressBar::chunk {{ background: {c("accent")}; border-radius: 4px; }}
    """
