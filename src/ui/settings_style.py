"""与应用亚克力表面一致的设置页文字和背景。"""

from PySide6.QtGui import QColor

from src.ui import icons, theme


def color(name: str) -> str:
    """取得设置页语义颜色，并按当前背景材质调整文字可读性。"""
    return theme.surface_text_color(f"settings.{name}")


def surface(name: str = "surface", role: str = "panel", *, over: str | None = None) -> str:
    """达到目标材质透明度，避免重复叠加底色。"""
    token = f"settings.{name}"
    if not theme.is_background_active() or over is None:
        return theme.surface_color(token, role)
    target = theme.ACRYLIC.get(role, theme.ACRYLIC["panel"])
    backing = theme.ACRYLIC[over]
    # 两层透明度按合成规则计算，不直接相加。
    alpha = max(0.0, (target - backing) / (1.0 - backing)) if backing < 1.0 else 0.0
    return theme.rgba(token, alpha)


def stroke_color(name: str = "line") -> QColor:
    """返回设置页边框颜色；背景启用时使用带指定透明度的主文字色。"""
    if not theme.is_background_active():
        return QColor(color(name))
    ink = QColor(theme.color("text.primary"))
    ink.setAlphaF(0.20 if name == "control" else 0.10)
    return ink


def stroke(name: str = "line") -> str:
    """将边框颜色转换为保留透明度的 QSS 颜色字符串。"""
    return stroke_color(name).name(QColor.NameFormat.HexArgb)


def stylesheet() -> str:
    """组合当前主题的设置页排版、层级表面、交互控件和滚动条样式表。"""
    c = color
    workspace = theme.surface_color("bg.layer", "nav") if theme.is_background_active() else c("canvas")

    def panel(name="surface"):
        """计算叠加在导航材质之上的设置面板颜色。"""
        return surface(name, over="nav")

    def control(name="surface"):
        """计算叠加在设置面板之上的高不透明度控件颜色。"""
        return surface(name, "strong", over="panel")

    line, control_line = stroke(), stroke("control")
    nav_text = theme.color("text.primary")
    # 交互底色在浅色主题加深、深色主题提亮，不受壁纸颜色影响。
    nav_hover = theme.rgba("text.primary", 0.08)
    nav_selected = theme.rgba("text.primary", 0.12)
    nav_selected_hover = theme.rgba("text.primary", 0.16)
    nav_radius = theme.radius("card")
    body = theme.font("size_body")
    secondary = theme.font("size_secondary")
    caption = theme.font("size_caption")
    arrow = (icons.write_themed_svg("actions/chevron-down", "settings.secondary") or "").replace("\\", "/")
    return f"""
        QWidget#settingsPage {{ background: transparent; color: {c("text")}; }}
        QWidget#settingsWorkspace {{ background: {workspace}; color: {c("text")};
            border: 1px solid {line}; border-radius: {theme.radius("large")}px; }}
        QWidget[settingsPlain="true"], QWidget#settingsPage QStackedWidget,
        QWidget#settingsPage QWidget#qt_scrollarea_viewport {{ background: transparent; border: none; }}
        QLabel[settingsRole="title"] {{ font-size: 23px; font-weight: 600; color: {c("text")}; }}
        QLabel[settingsRole="subtitle"] {{ font-size: {secondary}px; color: {c("secondary")}; }}
        QLabel[settingsRole="muted"] {{ font-size: {caption}px; color: {c("muted")}; }}
        QLabel[settingsRole="breadcrumb"] {{ font-size: {caption}px; color: {c("muted")}; }}
        QLabel[settingsRole="heading"] {{
            font-size: {theme.font("size_subtitle")}px;
            font-weight: 600;
            color: {c("text")};
            }}
        QLabel[settingsRole="label"] {{ font-size: {body}px; font-weight: 600; color: {c("text")}; }}
        QLabel[settingsRole="help"] {{ font-size: {secondary}px; color: {c("secondary")}; }}
        QLabel[settingsRole="smallHelp"] {{ font-size: {secondary}px; color: {c("secondary")}; }}
        QLabel[settingsRole="accent"] {{ font-size: {secondary}px; color: {c("accent")}; }}
        QLabel[settingsRole="badge"] {{ font-size: {caption}px; color: {c("muted")};
            border: 1px solid {line}; border-radius: 2px; padding: 0 4px; }}
        QLabel[settingsIcon="true"] {{ background: transparent; border: none; padding: 0; }}
        QFrame#settingsGroup {{ background: {panel()}; border: 1px solid {line}; border-radius: {nav_radius}px; }}
        QWidget#settingsCardHeader {{ background: transparent; border: none;
            border-bottom: 1px solid {line}; }}
        QWidget#settingsCardFooter {{ background: transparent; border: none;
            border-top: 1px solid {line}; }}
        QFrame#settingsRow {{ border: none; background: transparent; }}
        QFrame#settingsRow[settingsHighlight="true"], QFrame#settingsGroup[settingsHighlight="true"] {{
            border: 1px solid {c("accent")}; border-radius: 6px; }}
        QFrame#settingsDivider {{ background: {line}; border: none; }}
        QWidget#settingsToolbar {{ border: none; border-bottom: 1px solid {line}; background: transparent; }}
        QWidget#settingsTabs {{ border: none; background: transparent; }}
        QPushButton#settingsNavBtn {{ background: transparent; border: none; border-radius: {nav_radius}px;
            color: {nav_text}; font-size: {body}px; padding: 8px 10px 10px; }}
        QPushButton#settingsNavBtn:hover {{ background: {nav_hover}; color: {nav_text}; }}
        QPushButton#settingsNavBtn:checked {{ background: {nav_selected};
            color: {nav_text}; font-weight: 600; }}
        QPushButton#settingsNavBtn:checked:hover {{ background: {nav_selected_hover}; color: {nav_text}; }}
        QWidget#settingsNavIndicator {{ background: {c("accent")}; border-radius: 1px; }}
        QPushButton#settingsSectionButton {{ text-align: left; background: transparent; border: none;
            border-radius: {nav_radius}px; color: {nav_text}; font-size: {body}px; padding: 10px 12px; }}
        QPushButton#settingsSectionButton:hover {{ background: {nav_hover}; color: {nav_text}; }}
        QPushButton#settingsSectionButton:checked {{ background: {nav_selected};
            color: {nav_text}; font-weight: 600; }}
        QPushButton#settingsSectionButton:checked:hover {{ background: {nav_selected_hover}; color: {nav_text}; }}
        QWidget#settingsFooter {{ background: transparent; border: none; border-top: 1px solid {line}; }}
        QWidget#settingsSearchBox {{
            background: {panel()};
            border: 1px solid {control_line};
            border-radius: 6px;
            }}
        QWidget#settingsPage QLineEdit#settingsSearch {{ background: transparent; border: none; color: {c("text")};
            font-size: {secondary}px; padding: 3px 0; min-height: 28px; }}
        QLabel#settingsSearchKey {{ background: {control("soft")}; color: {c("muted")};
            font-size: {caption}px; border: 1px solid {line}; border-radius: 3px; padding: 1px 4px; }}
        QPushButton#settingsSearchResult {{ text-align: left; background: {panel()};
            border: 1px solid {line}; border-radius: 8px; padding: 0; min-height: 66px; }}
        QPushButton#settingsSearchResult:hover {{
            background: {surface("hover", "strong", over="nav")};
            border-color: {control_line};
            }}
        QWidget#settingsPage QLineEdit, QWidget#settingsPage QComboBox {{
            background: {control()}; color: {c("text")}; border: 1px solid {control_line};
            border-radius: 6px; min-height: 24px; padding: 5px 9px; font-size: {body}px; }}
        QWidget#settingsPage QLineEdit:focus,
        QWidget#settingsPage QComboBox:focus {{
            border-color: {c("accent")};
            }}
        QWidget#settingsPage QLineEdit:disabled, QWidget#settingsPage QComboBox:disabled {{
            background: {control("soft")}; color: {c("muted")}; }}
        QWidget#settingsPage QComboBox {{ padding-right: 27px; }}
        QWidget#settingsPage QComboBox::drop-down {{ width: 23px; background: transparent; border: none; }}
        QWidget#settingsPage QComboBox::down-arrow {{ image: url("{arrow}"); width: 12px; height: 12px; }}
        QWidget#settingsPage QComboBox QAbstractItemView {{ background: {c("surface")}; color: {c("text")};
            selection-background-color: {c("accent_soft")}; selection-color: {c("accent")}; }}
        QPushButton[settingsButton="standard"] {{ background: {control()}; color: {c("text")};
            border: 1px solid {control_line}; border-radius: 6px; padding: 6px 10px; font-size: {secondary}px; }}
        QPushButton[settingsButton="standard"]:hover {{ background: {control("hover")}; }}
        QPushButton[settingsButton="standard"]:checked {{
            background: {control("accent_soft")};
            color: {c("accent")};
            }}
        QPushButton[settingsButton="standard"]:disabled {{ color: {c("muted")}; background: {control("soft")}; }}
        QPushButton[settingsButton="link"] {{ color: {c("accent")}; background: transparent;
            border: none; font-size: {secondary}px; padding: 3px 0; }}
        QPushButton[settingsButton="link"]:disabled {{ color: {c("muted")}; }}
        QPushButton[settingsButton="link"]:hover {{ color: {c("text")}; }}
        QWidget#settingsSegments {{
            background: {surface("soft", over="panel")};
            border: 1px solid {line};
            border-radius: 7px;
            }}
        QPushButton#settingsSegment {{ background: transparent; color: {c("secondary")};
            border: 1px solid transparent; border-radius: 4px; padding: 6px 10px; font-size: {secondary}px; }}
        QPushButton#settingsSegment:checked {{ background: {control()}; border-color: {line};
            color: {c("accent")}; font-weight: 600; }}
        QPushButton#settingsSegment:hover {{ color: {c("accent")}; }}
        QPushButton#settingsThemeChoice {{ background: {control()}; border: 1px solid {line};
            border-radius: 8px; min-height: 102px; padding: 0; }}
        QPushButton#settingsThemeChoice:checked {{ border: 2px solid {c("accent")}; }}
        QLabel[settingsRole="metric"] {{ font-size: 34px; font-weight: 500; color: {c("text")}; }}
        QLabel[settingsRole="warning"] {{ color: {c("warning")}; font-size: {secondary}px;
            background: {control("warning_bg")}; border-radius: 6px; padding: 8px 10px; }}
        QLabel[settingsRole="error"] {{ color: {c("danger")}; font-size: {secondary}px; }}
        QProgressBar#settingsUsageBar {{ border: none; border-radius: 3px; background: {c("line")}; }}
        QProgressBar#settingsUsageBar::chunk {{ border: none; border-radius: 3px; background: {c("accent")}; }}
        QWidget#settingsPage QSlider {{ background: transparent; border: none; min-height: 20px; }}
        QWidget#settingsPage QSlider::groove:horizontal {{
            height: 5px;
            border: none;
            background: {c("line")};
            border-radius: 2px;
            }}
        QWidget#settingsPage QSlider::sub-page:horizontal {{ background: {c("accent")}; border-radius: 2px; }}
        QWidget#settingsPage QSlider::handle:horizontal {{ width: 10px; height: 10px; margin: -6px 0;
            border: 4px solid {c("surface")}; background: {c("accent")}; border-radius: 8px; }}
        QWidget#settingsPage QScrollArea, QWidget#settingsCategory, QWidget#settingsTopic {{
            border: none; background: transparent; }}
        QWidget#settingsPage QScrollBar:vertical {{ background: transparent; width: 8px; margin: 2px; }}
        QWidget#settingsPage QScrollBar::handle:vertical {{
            background: {c("control")};
            border-radius: 3px;
            min-height: 28px;
            }}
        QWidget#settingsPage QScrollBar::add-line:vertical,
        QWidget#settingsPage QScrollBar::sub-line:vertical {{
            height: 0;
            }}
        QWidget#settingsPage QScrollBar::add-page:vertical,
        QWidget#settingsPage QScrollBar::sub-page:vertical {{
            background: transparent;
            }}
    """
