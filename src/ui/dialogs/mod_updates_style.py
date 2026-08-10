"""更新对话框的局部样式，适应不同窗口大小。"""

from PySide6.QtGui import QColor

from src.ui import theme

BODY_FONT_SIZE = 14
TEXT_LINE_HEIGHT = 22


def color(name: str) -> str:
    """读取 mod_updates 命名空间下的主题颜色，供更新界面共享统一色值。"""
    return theme.color(f"mod_updates.{name}")


def blend(foreground: str, background: str, opacity: float) -> str:
    """按前景比例混合两个更新主题色，返回用于不透明背景的十六进制颜色。"""
    front, back = QColor(color(foreground)), QColor(color(background))
    return QColor(
        *(
            round(a * opacity + b * (1 - opacity))
            for a, b in zip((front.red(), front.green(), front.blue()), (back.red(), back.green(), back.blue()))
        )
    ).name()


def stylesheet() -> str:
    """生成更新窗口的局部控件样式，颜色随当前主题解析。"""
    return f"""
        QWidget {{
            font-size: {BODY_FONT_SIZE}px; color: {color("text")}; background: transparent; border: 0;
        }}
        QLabel {{ padding: 0; margin: 0; background: transparent; }}
        QPushButton {{
            padding: 0; margin: 0;
            min-height: 32px; max-height: 32px;
            border: 1px solid {color("control")}; border-radius: 5px;
            background: {color("surface")};
        }}
        QPushButton:hover {{ background: {color("hover")}; }}
        QPushButton:focus {{ border-color: {color("accent")}; }}
        QPushButton:disabled {{ color: {color("muted")}; }}
        QPushButton[quiet="true"], QPushButton[iconOnly="true"] {{
            border-color: transparent; background: transparent; color: {color("secondary")};
        }}
        QPushButton[quiet="true"]:hover, QPushButton[iconOnly="true"]:hover {{
            background: {color("hover")};
        }}
        QPushButton[quiet="true"]:focus, QPushButton[iconOnly="true"]:focus {{
            border-color: {color("accent")};
        }}
        QPushButton::menu-indicator {{ image: none; width: 0; height: 0; }}
        QLabel#updateWindowName {{ color: {color("muted")}; }}
        QPushButton#updateClose {{
            min-height: 28px; max-height: 28px; border-radius: 0; border-top-right-radius: 9px;
        }}
        QPushButton#updateClose:hover {{ background: #c42b1c; color: white; }}
        QPushButton#updateRecheck:disabled {{ color: {color("accent")}; }}
        QLabel#updateHeaderIcon {{
            border: 1px solid {color("accent_border")}; border-radius: 12px;
            background: {color("accent_soft")};
        }}
        QLabel#updateTitle {{ font-size: 22px; font-weight: 600; }}
        QLabel#updateSubtitle {{ color: {color("secondary")}; }}
        QWidget#updateBody {{ border-top: 1px solid {color("divider")}; }}
        QLabel#updateCollectionLabel {{ color: {color("secondary")}; }}
        QLabel#updateVisibleCount {{
            color: {color("secondary")}; background: {color("subtle")};
            border-radius: 4px; padding: 0 5px;
        }}
        QLineEdit#updateSearch {{
            border: 1px solid {color("line")}; border-radius: 5px;
            background: {color("layer")}; padding: 0 7px;
            selection-background-color: {color("accent")}; selection-color: {color("on_accent")};
        }}
        QLineEdit#updateSearch:focus {{ border-color: {color("accent")}; }}
        QScrollArea, QScrollArea>QWidget, QScrollArea>QWidget>QWidget {{
            background: transparent; border: 0; padding: 0; margin: 0;
        }}
        QScrollBar:vertical {{ width: 12px; background: transparent; margin: 3px 6px 3px 0; border: 0; }}
        QScrollBar::handle:vertical {{ background: {color("control")}; border-radius: 3px; min-height: 28px; }}
        QScrollBar::handle:vertical:hover {{ background: {color("muted")}; }}
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
        QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: none; }}
        QWidget#updateRow {{ border-bottom: 1px solid {color("divider")}; }}
        QWidget#updateRow[last="true"] {{ border-bottom: 0; }}
        QWidget#updateRow[complete="true"] {{
            background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                stop:0 {blend("green_soft", "surface", 0.68)}, stop:.92 {color("surface")});
            border-radius: 7px;
        }}
        QLabel#updateName {{ font-weight: 600; }}
        QLabel#updateThumb {{ color: {color("muted")}; }}
        QLabel#updateOldVersion {{ color: {color("secondary")}; }}
        QLabel#updateNewVersion {{
            color: {color("accent")}; background: {color("accent_soft")};
            border-radius: 4px;
        }}
        QLabel#updateNewVersion[complete="true"] {{
            color: {color("green")}; background: {color("green_soft")};
        }}
        QLabel#updateFileSize, QLabel#updateDateHint {{ color: {color("muted")}; }}
        QWidget#updateVersionDivider {{ background: {color("line")}; }}
        QPushButton#updateSummary {{
            text-align: left; color: {color("secondary")};
            border: 0; background: transparent;
            min-height: {TEXT_LINE_HEIGHT}px; max-height: {TEXT_LINE_HEIGHT}px;
        }}
        QPushButton#updateSummary:hover, QPushButton#updateSummary:focus {{ color: {color("accent")}; }}
        QPushButton#updateSingle, QPushButton#updateMore {{ min-height: 28px; max-height: 28px; }}
        QPushButton#updateSingle {{ color: {color("accent")}; border-color: {color("accent_border")}; }}
        QPushButton#updateSingle:hover {{ background: {color("accent_soft")}; }}
        QPushButton#updateSingle:disabled {{
            color: {blend("accent", "surface", 0.48)}; border-color: {blend("accent_border", "surface", 0.48)};
        }}
        QPushButton#updateSingle[state="updating"], QPushButton#updateSingle[state="done"] {{
            border-color: transparent; background: transparent;
        }}
        QPushButton#updateSingle[state="updating"] {{ color: {color("accent")}; }}
        QPushButton#updateSingle[state="done"] {{ color: {color("green")}; }}
        QPushButton#updateTaskStop {{
            min-height: 26px; max-height: 26px; border-radius: 5px;
        }}
        QPushButton#updateTaskStop:hover, QPushButton#updateBatchStop:hover {{
            background: {color("warning_soft")}; border-color: {color("warning_border")};
        }}
        QLabel#updateTaskPhase {{ font-weight: 600; }}
        QLabel#updateTaskPercent, QLabel#updateTaskMetrics, QLabel#updateTaskDetail {{
            color: {color("secondary")};
        }}
        QLabel#updateTaskFile {{ color: {color("muted")}; }}
        QWidget#updateBatchSurface {{
            border: 1px solid {color("line")}; border-radius: 6px; background: {color("layer")};
        }}
        QLabel#updateBatchTitle {{ font-weight: 600; }}
        QLabel#updateBatchCounts, QLabel#updateBatchTransfer {{ color: {color("secondary")}; }}
        QPushButton[batchControl="true"] {{
            min-height: 22px; max-height: 22px;
            border-color: transparent; background: transparent; color: {color("secondary")};
        }}
        QPushButton[batchControl="true"]:hover {{ background: {color("hover")}; }}
        QPushButton[batchControl="true"]:focus {{ border-color: {color("accent")}; }}
        QPushButton[batchControl="true"]:disabled {{ color: {color("muted")}; }}
        QWidget#updateLog {{ border-left: 2px solid {color("accent_border")}; }}
        QLabel#updateLogTitle {{ font-weight: 600; }}
        QLabel#updateLogDate {{ color: {color("muted")}; }}
        QLabel#updateLogTag {{
            color: {color("secondary")}; background: {color("subtle")};
            border: 1px solid {color("line")}; border-radius: 3px;
        }}
        QLabel#updateLogText {{ color: {color("secondary")}; }}
        QPushButton#updateSourceLink {{
            color: {color("accent")}; text-align: left;
            border: 0; background: transparent; min-height: 24px; max-height: 24px;
        }}
        QPushButton#updateSourceLink:hover {{ text-decoration: underline; }}
        QWidget#updateFooter {{
            background: {color("layer")}; border-top: 1px solid {color("divider")};
            border-bottom-left-radius: 9px; border-bottom-right-radius: 9px;
        }}
        QLabel#updateFooterCount {{ color: {color("secondary")}; }}
        QLabel#updateDownloadSize {{ color: {color("muted")}; }}
        QPushButton#updateAll {{
            background: {color("accent")}; border-color: {color("accent")}; min-height: 34px; max-height: 34px;
        }}
        QPushButton#updateAll:hover {{ background: {color("accent_hover")}; }}
        QPushButton#updateAll:disabled {{
            background: {blend("accent", "layer", 0.48)}; border-color: {blend("accent", "layer", 0.48)};
        }}
        QPushButton#updateAll QLabel {{ color: {color("on_accent")}; font-weight: 600; }}
        QPushButton#updateAll:disabled QLabel {{ color: {blend("on_accent", "layer", 0.48)}; }}
        QWidget#updateAllDivider {{ background: {color("on_accent")}; }}
        QLabel#updateEmptyIcon {{
            background: {color("accent_soft")}; border: 1px solid {color("accent_border")}; border-radius: 20px;
        }}
        QLabel#updateEmptyIcon[complete="true"] {{
            background: {color("green_soft")}; border-color: {blend("green", "line", 0.28)};
        }}
        QLabel#updateEmptyTitle {{ font-size: 18px; font-weight: 600; }}
        QLabel#updateEmptyDescription {{ color: {color("secondary")}; }}
        QPushButton#updateEmptyAction {{
            background: {color("accent_soft")}; border-color: {color("accent_border")}; color: {color("accent")};
        }}
        QWidget#updateMessage {{
            background: {color("surface")}; border: 1px solid {color("control")}; border-radius: 8px;
        }}
        QPushButton#updateUndo {{
            color: {color("accent")}; border: 0; background: transparent;
            min-height: 34px; max-height: 34px;
        }}
    """


def menu_stylesheet() -> str:
    """生成更新行弹出菜单样式，使独立菜单与当前窗口主题保持一致。"""
    return f"""
        QMenu {{
            background: {color("surface")}; color: {color("text")};
            border: 1px solid {color("control")}; border-radius: 7px; padding: 5px;
            font-size: {BODY_FONT_SIZE}px;
        }}
        QMenu::item {{ padding: 9px 15px 9px 10px; border-radius: 4px; }}
        QMenu::item:selected {{ background: {color("hover")}; }}
        QMenu::item:disabled {{ color: {color("muted")}; }}
        QMenu::separator {{ height: 1px; background: {color("divider")}; margin: 4px 5px; }}
        QMenu::icon {{ padding-left: 7px; }}
        QLabel {{
            font-size: {BODY_FONT_SIZE}px; color: {color("muted")};
            padding: 5px 10px 6px; background: transparent;
        }}
    """
