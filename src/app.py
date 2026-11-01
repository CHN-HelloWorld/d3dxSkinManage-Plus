"""应用入口 - 主题切换、i18n 初始化"""

from __future__ import annotations

import sys
from time import perf_counter

from PySide6.QtCore import QLoggingCategory
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from src.core.config import Config
from src.utils.constants import (
    APP_DISPLAY_NAME,
    APP_NAME,
    APP_ORGANIZATION,
    DEFAULT_WINDOW_HEIGHT,
    DEFAULT_WINDOW_WIDTH,
)
from src.utils.elevation import ensure_elevated
from src.utils.i18n import set_language
from src.utils.logger import logger
from src.utils.paths import get_resources_dir


class App:
    """应用程序类 - 管理 QApplication 生命周期"""

    _instance: App | None = None

    def __new__(cls) -> App:
        """创建并缓存进程内唯一的应用协调对象。"""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        """首次构造时载入配置并初始化 Qt 应用和主窗口引用。"""
        if self._initialized:
            return
        self._initialized = True
        self._config = Config()
        self._qt_app: QApplication | None = None
        self._main_window = None

    @classmethod
    def instance(cls) -> App:
        """返回进程内共享的应用协调对象，首次访问时构造。"""
        return cls()

    def run(self) -> int:
        """启动应用"""
        startup_started = perf_counter()
        # 初始化函数可重复调用，保证直接启动应用时也记录日志。
        from src.utils.logger import init_app_logging, install_qt_message_handler
        from src.utils.win_console import disable_quick_edit_mode

        # 禁用会阻塞标准输出的控制台快速编辑模式；无控制台时无需处理。
        disable_quick_edit_mode()
        init_app_logging()
        lang = self._config.get("general.language", "zh-CN")
        set_language(lang)
        # Qt 日志通过 qInstallMessageHandler 接入，需在创建 QApplication 前安装，
        # 确保无控制台启动时也能记录初始化错误。
        install_qt_message_handler()
        logger.info("=" * 56)
        logger.info("应用启动")

        # 提权先于 QApplication 和文件监控初始化，避免进程切换留下未释放的资源。
        # 日志已初始化，可记录提权失败；打包程序由可执行文件清单请求管理员权限。
        exit_code = ensure_elevated(APP_DISPLAY_NAME)
        if exit_code is not None:
            return exit_code

        # 禁用图像格式探测的无效警告。
        # 禁用字体回退链中的缺失 OpenType 支持警告。

        QLoggingCategory.setFilterRules(
            "qt.gui.imageio.warning=false\nqt.text.font.db.warning=false\nqt.qpa.fonts.warning=false"
        )
        # Windows：显式设置 AppUserModelID，让任务栏用应用自身图标并正确分组，
        # 而不是继承 python.exe / 启动器 的图标（必须在创建任何窗口之前调用）。
        if sys.platform == "win32":
            try:
                import ctypes

                ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("d3dxSkinManage-Plus.App")
            except Exception:
                pass

        phase_started = perf_counter()
        self._qt_app = QApplication(sys.argv)
        self._qt_app.setApplicationName(APP_NAME)
        # 不设 applicationDisplayName：Qt 会把它作为「 - 后缀」追加到每个已自设标题的弹窗后
        # （如「删除 Mod - d3dxSkinManage-Plus」）。显式置空以抑制该后缀——留空会回退到
        # applicationName（同名），故必须置 ""、不能只删本行。主窗标题由 main_window 自行设。
        self._qt_app.setApplicationDisplayName("")
        self._qt_app.setOrganizationName(APP_ORGANIZATION)

        from src.ui.application_event_filters import group_application_event_filters

        # 连续安装的三个过滤器作为同一组，
        # 在独立拖放过滤器之前结束，保持 Qt 原有的调用顺序。
        with group_application_event_filters(self._qt_app):
            # 所有页面、弹窗及插件的下拉框只通过显式选择切项，避免滚动浏览时误改。
            from src.ui import combo_wheel

            combo_wheel.install(self._qt_app)

            # 应用图标（窗口标题栏 / 任务栏 / Alt+Tab）——resources/app.ico 为多尺寸
            # (16~256)；scripts/release/build.py 打包时使用同一文件作为可执行文件图标。
            _icon_path = get_resources_dir() / "app.ico"
            if _icon_path.exists():
                _app_icon = QIcon(str(_icon_path))
                if not _app_icon.isNull():
                    self._qt_app.setWindowIcon(_app_icon)

            # 背景图模式下让右键菜单 / 下拉弹窗呈真半透明亚克力（app 级事件过滤器）
            from src.ui import acrylic

            acrylic.install(self._qt_app)
            # 通过应用事件过滤器让 QMessageBox 和 QDialog 标题栏跟随主题。
            from src.ui import win_effects as _we

            _we.install_dialog_titlebar_theming(self._qt_app)
        # 以管理员身份运行时，UIPI 会阻止资源管理器向本进程传递 OLE 拖放。
        # 使用 Shell 的 WM_DROPFILES 通道处理拖放导入；未提权时无需安装该通道。
        from src.ui import win_dnd_admin

        win_dnd_admin.install(self._qt_app)

        # 初始化 i18n
        # Qt 内置控件（QMessageBox 的 Save/Discard/Yes/No/Cancel 标准按钮、右键菜单等）
        # 的文案由 Qt 自带翻译提供；不装 QTranslator 时全部回退英文。随语言装载对应的
        # qtbase_*.qm，使所有标准按钮统一使用界面语言。
        self._install_qt_translations(lang)

        with group_application_event_filters(self._qt_app):
            # 字体先于主题与主窗口初始化，QSS 与自绘控件使用同一字体来源。
            self.apply_font(self._config.get("appearance.font_family", ""))

            # 应用主题
            theme = self._config.get("general.theme", "light")
            self.apply_theme(theme)
        logger.info("启动阶段：Qt 与主题初始化 %.3f 秒", perf_counter() - phase_started)

        # 创建主窗口
        phase_started = perf_counter()
        from src.ui.main_window import MainWindow

        logger.info("启动阶段：主窗口模块载入 %.3f 秒", perf_counter() - phase_started)
        self._main_window = MainWindow()
        self._main_window.resize(DEFAULT_WINDOW_WIDTH, DEFAULT_WINDOW_HEIGHT)
        self._main_window.show()

        # Win11 Mica / 沉浸式标题栏（最佳努力，失败静默回退）
        try:
            from src.ui import theme as theme_mod
            from src.ui import win_effects

            win_effects.apply_mica(self._main_window, theme_mod.current_theme())
        except Exception as e:
            logger.debug("窗口视觉增强不可用: %s", e)

        logger.info("应用启动完成（准备耗时 %.3f 秒）", perf_counter() - startup_started)
        return self._qt_app.exec()

    def _install_qt_translations(self, lang: str) -> None:
        """为 Qt 标准控件加载对应语言的官方翻译。

        应用持有翻译器引用，避免对象回收后翻译失效；英文界面使用 Qt 原始文本。
        """
        from PySide6.QtCore import QLibraryInfo, QLocale, QTranslator

        from src.ui.i18n import LanguagePackTranslator
        from src.utils.i18n import get_i18n

        # 界面语言 → Qt locale 名（qtbase_*.qm 用下划线）
        qt_locale = QLocale(get_i18n().current_language.replace("-", "_"))
        # 已装的旧翻译器先卸掉，避免切换语言后叠加
        for old in getattr(self, "_qt_translators", []):
            self._qt_app.removeTranslator(old)
        self._qt_translators: list[QTranslator] = []

        try:
            tr_dir = QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)
        except Exception:
            tr_dir = ""
        # qtbase 覆盖大部分标准按钮/通用文案；qt_* 为聚合包，一并尝试装载。
        for base in ("qtbase", "qt"):
            translator = QTranslator(self._qt_app)
            if translator.load(qt_locale, base, "_", tr_dir):
                self._qt_app.installTranslator(translator)
                self._qt_translators.append(translator)
        override = LanguagePackTranslator(get_i18n(), self._qt_app)
        self._qt_app.installTranslator(override)
        self._qt_translators.append(override)

    def apply_font(self, family: str = "", *, on_finished=None) -> None:
        """字体独立于换肤；实时切换分批更新，启动时可同步应用。"""
        if self._qt_app is None:
            if on_finished is not None:
                on_finished()
            return
        from src.ui.fonts import apply_font, apply_font_live, resolve_font

        font = resolve_font(family)
        if on_finished is None:
            apply_font(self._qt_app, font)
            logger.info("界面字体已应用: %s", font.family())
        else:

            def finished() -> None:
                """在字体刷新完成后记录结果并调用调用方的完成回调。"""
                logger.info("界面字体已应用: %s", font.family())
                on_finished()

            apply_font_live(self._qt_app, font, finished)

    def apply_theme(self, theme_name: str, *, on_finished=None) -> bool:
        """应用主题/材质；预置明暗 QSS，实时颜色与材质切换可分批完成。

        返回是否有样式变化；可选回调收到同一 bool，供通知等待分批刷新完成。
        """
        if self._qt_app is None:
            if on_finished is not None:
                on_finished(False)
            return False

        from src.ui import icons
        from src.ui import theme as theme_mod
        from src.ui.background_style import appearance_stylesheet, install_background_style

        surfaces = install_background_style(self._qt_app)
        previous_theme = theme_mod.current_theme()
        resolved = theme_mod.set_theme(theme_name)
        try:
            # 先同步背景启用状态，使内联表面颜色与背景覆盖样式一致。
            bg_on = bool(self._config.get("appearance.background.enabled", False))
            controller = getattr(self._main_window, "_background_controller", None)
            if controller is not None:
                bg_on = controller.active
            else:
                from src.ui.backgrounds.config import background_options

                bg = background_options(self._config)
                bg_on = bg["source"] == "image" and bool(bg["path"])
            theme_mod.set_background_active(bg_on)
            qss = appearance_stylesheet()
            qss_changed = qss != self._qt_app.styleSheet()
            changed = qss_changed or previous_theme != resolved or surfaces.enabled != bg_on
            if previous_theme != resolved:
                icons.clear_cache()
            if qss_changed:
                self._qt_app.setStyleSheet(qss)
            if changed:
                logger.info("主题已应用 (Fluent): %s", resolved)
            if on_finished is None:
                surfaces.apply(bg_on, theme_name=resolved)
            else:
                surfaces.apply(bg_on, theme_name=resolved, on_finished=lambda: on_finished(changed))
        except Exception as e:
            logger.error("加载 Fluent 主题失败: %s", e)
            if on_finished is not None:
                on_finished(False)
            return False

        # 已显示的主窗口需同步 Mica/标题栏明暗
        if self._main_window is not None and previous_theme != resolved:
            try:
                from src.ui import win_effects

                win_effects.apply_mica(self._main_window, resolved)
                # 主题变化时刷新已打开对话框的标题栏明暗。
                win_effects.refresh_open_dialog_titlebars(resolved == "dark")
            except Exception as e:
                logger.debug("窗口视觉增强不可用: %s", e)
        return changed

    def set_language(self, lang: str) -> None:
        """切换语言 (需要重启 UI 才能完全生效)"""
        set_language(lang)
        logger.info("语言已切换: %s", lang)
