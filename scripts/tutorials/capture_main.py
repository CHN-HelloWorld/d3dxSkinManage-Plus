"""使用真实 PySide6 控件生成最终版教程截图，所有业务数据均位于临时演示目录。"""

from __future__ import annotations

import argparse
import json
import os
import time
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from demo_common import GAME_ID, TUTORIAL, isolate
from image_assets import save_qt_capture

os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["QT_LOGGING_RULES"] = "qt.multimedia.*=false;qt.qpa.*=false"


def pump(app, milliseconds: int = 400) -> None:
    """在有限时间内处理界面事件，让后台图片和布局完成。"""
    deadline = time.monotonic() + milliseconds / 1000
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)


def wait_until(app, condition, *, timeout: float = 30) -> None:
    """等待明确的后台完成条件，超时即中止截图。"""
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() >= deadline:
            raise TimeoutError("截图等待后台任务超时。")
        pump(app, 30)
    pump(app, 120)


def offline_request(*args, **kwargs):
    """禁止演示进程发出真实 HTTP 请求。"""
    raise RuntimeError("教程演示进程不访问网络。")


async def offline_async_request(*args, **kwargs):
    """禁止演示进程发出真实异步 HTTP 请求。"""
    raise RuntimeError("教程演示进程不访问网络。")


def capture(theme_name: str, selected: set[str], *, language: str = "zh-CN", scale: int = 2) -> None:
    """依次展示程序页面和对话框，保存所选场景后完整释放窗口及线程。"""
    os.environ["QT_SCALE_FACTOR"] = str(scale)
    workspace = isolate()
    # Qt 离屏默认屏幕为 800×800 物理像素。提高 DPR 时同步扩大物理屏幕，
    # 保持原来的逻辑可用区域，避免带 fit-to-screen 的弹窗缩成一半。
    screen_config = workspace / "capture-screen.json"
    screen_config.write_text(
        json.dumps(
            {
                "screens": [
                    {
                        "name": "tutorial",
                        "width": 800 * scale,
                        "height": 800 * scale,
                        "logicalDpi": 96,
                        "logicalBaseDpi": 96,
                        "dpr": 1,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    os.chdir(workspace)
    os.environ["QT_QPA_PLATFORM"] = "offscreen:configfile=capture-screen.json"
    data = json.loads((workspace / "demo.json").read_text(encoding="utf-8"))
    import httpx
    from PySide6.QtCore import QLoggingCategory, QPoint, Qt
    from PySide6.QtGui import QFont, QFontDatabase
    from PySide6.QtWidgets import QAbstractButton, QApplication, QComboBox, QLabel, QLineEdit, QWidget

    from src.core.config import Config
    from src.ui import anim, combo_wheel, theme
    from src.utils.i18n import set_language, t
    from src.utils.logger import init_app_logging, logger

    init_app_logging()
    logger.setLevel("WARNING")
    QLoggingCategory.setFilterRules(os.environ["QT_LOGGING_RULES"])
    if not set_language(language):
        raise RuntimeError(f"演示环境缺少 {language} 语言包，请先运行 prep_workspace.py。")
    Config().set("general.theme", theme_name)
    Config().set("general.language", language)
    app = QApplication.instance() or QApplication(["tutorial-capture"])
    # Qt 离屏后端不枚举 Windows 字体，需要显式注册中文及西文字体。
    for filename in ("msyh.ttc", "msyhbd.ttc", "segoeui.ttf", "consola.ttf"):
        font_file = Path("C:/Windows/Fonts") / filename
        if font_file.is_file():
            QFontDatabase.addApplicationFont(str(font_file))
    if "Microsoft YaHei UI" not in QFontDatabase.families():
        raise RuntimeError("无法加载中文字体，停止生成可能缺字的截图。")
    app.setFont(QFont("Microsoft YaHei UI", 10))
    combo_wheel.install(app)
    anim.set_enabled(False)
    theme.set_theme(theme_name)
    app.setStyleSheet(theme.render_qss(theme_name))
    folder = "final" if theme_name == "dark" else "final-light"
    if language == "en-US":
        folder += "-en"
    output = TUTORIAL / "images" / folder
    output.mkdir(parents=True, exist_ok=True)
    records = []

    def want(number):
        """判断当前场景是否在用户选择范围内。"""
        return not selected or number in selected

    def shot(name, widget, *, settle=600, required_text=()):
        """仅在控件可见且截图非空时写入无损 WebP 和尺寸记录。"""
        pump(app, settle)
        pixmap = widget.grab()
        if pixmap.isNull():
            raise RuntimeError(f"截图失败：{name}")
        controls = []
        for child in widget.findChildren(QWidget):
            if (
                not isinstance(child, (QAbstractButton, QComboBox, QLabel, QLineEdit))
                or not child.isVisible()
                or child.window() is not widget.window()
                or child.visibleRegion().isEmpty()
            ):
                continue
            value = child.currentText() if isinstance(child, QComboBox) else child.text()
            if not value.strip():
                continue
            pos = child.mapTo(widget, QPoint(0, 0))
            if (
                pos.x() < 0
                or pos.y() < 0
                or pos.x() + child.width() > widget.width()
                or pos.y() + child.height() > widget.height()
            ):
                continue
            controls.append(
                {
                    "class": type(child).__name__,
                    "text": value,
                    "box": [pos.x(), pos.y(), child.width(), child.height()],
                    "enabled": child.isEnabled(),
                }
            )
        visible_text = {item["text"] for item in controls}
        for expected in required_text:
            if expected not in visible_text and t(expected) not in visible_text:
                raise RuntimeError(f"场景 {name} 缺少必要控件：{expected}")
        save_qt_capture(pixmap, output / f"{name}.webp")
        records.append(
            {
                "file": f"{name}.webp",
                "width": pixmap.width(),
                "height": pixmap.height(),
                "logical_width": widget.width(),
                "logical_height": widget.height(),
                "device_pixel_ratio": pixmap.devicePixelRatio(),
                "language": language,
                "controls": controls,
                "required_text": list(required_text),
            }
        )
        print(f"已截图：{name}（{pixmap.width()} × {pixmap.height()}）", flush=True)

    def show_dialog(dialog, name, *, ready=None):
        """非阻塞显示并抓取真实对话框，关闭前等待其自身后台任务结束。"""
        dialog.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        dialog.show()
        if ready is not None:
            wait_until(app, ready)
        shot(name, dialog, settle=900)
        dialog.reject()
        pump(app, 250)
        if hasattr(dialog, "busy"):
            wait_until(app, lambda: not dialog.busy)
        dialog.deleteLater()
        pump(app, 150)

    with ExitStack() as stack:
        stack.enter_context(patch.object(httpx.Client, "send", offline_request))
        stack.enter_context(patch.object(httpx.AsyncClient, "send", offline_async_request))
        from src.ui.main_window import MainWindow

        stack.enter_context(patch.object(MainWindow, "_maybe_startup_update_check", lambda self: None))
        win = MainWindow()
        win.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        win.resize(1440, 960)
        win.show()
        try:
            pump(app, 1600)
            wait_until(app, lambda: win._intake_worker is None and not win._intake_settling)
            manager = win._mod_manager
            game_page = win._game_page
            page = win._overview_page
            settings = win._settings_page
            win._select_nav_page(MainWindow.PAGE_GAME)
            game_page._select(GAME_ID)
            if want("02"):
                shot("02-游戏配置", win)
            if want("01"):
                game_page._on_add()
                detail = game_page._detail
                detail._name.setText("第二个示例游戏")
                detail._name_en.setText("Sample Game 2")
                detail._migoto.setText("D:/ModLoader")
                detail._mods.setText("D:/ModLoader/Mods")
                shot("01-添加游戏", win)
                game_page._exitchapters(silent=True)
                game_page._select(GAME_ID)
            win._select_nav_page(MainWindow.PAGE_OVERVIEW)
            page._navigate_to("")
            if want("03"):
                shot("03-类别总览", win)
            if want("30"):
                # 使用总览的真实空库呈现入口，仅替换页面快照，不修改演示库。
                existing_categories = list(page._all_characters)
                page.set_characters([], force_root=True)
                shot("30-新建类别入口", win, required_text=("+ 新建类别",))
                page.set_characters(existing_categories, force_root=True)
            if want("31"):
                from src.ui.pages.overview import CharacterDialog

                dialog = CharacterDialog(parent=win)
                dialog._name_input.setText("外观")
                dialog._name_en_input.setText("Appearance")
                dialog.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
                dialog.show()
                shot("31-新建类别", dialog, required_text=("新建类别", "节点类型", "确认", "外观"))
                dialog.reject()
                dialog.deleteLater()
                pump(app, 150)
            if want("32"):
                page._navigate_to(data["nodes"]["待整理"])
                shot("32-导入入口", win, required_text=("导入 Mod",))

            page._navigate_to(data["nodes"]["外观"])
            pump(app, 800)
            if want("33"):
                page._on_mod_card_clicked(data["mods"][1])
                shot("33-启用Mod", win, required_text=("启用",))
            page._on_mod_card_clicked(data["mods"][0])
            pump(app, 1000)
            page._detail_tabs.setCurrentIndex(0)
            if want("04"):
                shot("04-Mod信息", win)
            if want("06"):
                page._detail_tabs.setCurrentIndex(1)
                shot("06-按键设置", win)
            if want("07"):
                page._detail_tabs.setCurrentIndex(2)
                shot("07-变体管理", win)
            page._detail_tabs.setCurrentIndex(0)
            if want("08"):
                selector = page._preset_selector
                selector._show_popup()
                shot("08-组合预设", selector._popup)
                selector._popup.hide()
            if want("05"):
                from src.ui.dialogs.mod_import import ModImportDialog

                candidates = sorted((workspace / "demo/待导入").glob("*.zip"))[:2]
                dialog = ModImportDialog(data["nodes"]["外观"], [str(p) for p in candidates], win)
                show_dialog(
                    dialog,
                    "05-导入Mod",
                    ready=lambda: dialog._scan_worker is None or not dialog._scan_worker.isRunning(),
                )
            if want("09"):
                from src.ui.dialogs.mod_edit import ModEditDialog

                show_dialog(ModEditDialog(manager.get_mod(data["mods"][0]), win), "09-编辑Mod")
            if want("22"):
                from src.ui.pages.overview import BatchAddCharacterDialog

                show_dialog(BatchAddCharacterDialog(win), "22-批量添加")
            if want("10") or want("25"):
                from src.ui.dialogs.category_preset import CategoryPresetDialog

                if want("10"):
                    show_dialog(CategoryPresetDialog(manager, GAME_ID, win), "10-分类预设")
                if want("25"):
                    dialog = CategoryPresetDialog(manager, GAME_ID, win, start_export=True)
                    show_dialog(dialog, "25-分类导出", ready=lambda: not dialog.busy)
            if want("20"):
                from src.ui.dialogs.game_delete import GameDeleteDialog

                root = workspace / "data" / GAME_ID
                info = {
                    "mods": len(data["mods"]),
                    "characters": len(data["nodes"]),
                    "presets": len(manager.preset_manager.get_all_presets(GAME_ID)),
                    "deployed": 1,
                    "library_dir": str(root),
                    "library_size": sum(path.stat().st_size for path in root.rglob("*") if path.is_file()),
                }
                show_dialog(GameDeleteDialog("示例游戏", info, win), "20-删除游戏")
            win._select_nav_page(MainWindow.PAGE_SETTINGS)
            for number, index, name in (
                ("11", 0, "11-设置常规"),
                ("12", 1, "12-设置个性化"),
                ("13", 2, "13-快捷键"),
                ("14", 3, "14-导入与导出"),
                ("15", 4, "15-在线设置"),
                ("16", 5, "16-程序更新"),
            ):
                if want(number):
                    settings._settings_nav_btns[index].click()
                    shot(name, win)
            if want("26"):
                settings._settings_search.setText("cache" if language == "en-US" else "缓存")
                shot("26-设置搜索", win)
                settings._settings_search.clear()
            for number, index, section, name in (
                ("27", 0, "cache", "27-图片与缓存"),
                ("28", 1, "background", "28-整窗背景"),
            ):
                if want(number):
                    settings._settings_nav_btns[index].click()
                    settings._stack.widget(index).select_section(section)
                    shot(name, win)
            for number, index, name in (
                ("17", MainWindow.PAGE_TOOLS, "17-工具箱"),
                ("18", MainWindow.PAGE_PLUGINS, "18-插件"),
                ("19", MainWindow.PAGE_ABOUT, "19-关于"),
                ("21", MainWindow.PAGE_GALLERY, "21-画廊"),
            ):
                if want(number):
                    if index == MainWindow.PAGE_ABOUT:
                        win._about_btn.click()
                    else:
                        win._select_nav_page(index)
                    wait_until(app, lambda: win._stack.currentIndex() == index)
                    shot(name, win, settle=1000)
            if want("23"):
                from src.ui.dialogs.log_viewer import LogViewerDialog

                show_dialog(LogViewerDialog(win), "23-日志")
            if want("24") or want("29"):
                from capture_online import capture_online

                capture_online(win, workspace, shot, want)
            manifest_path = output / "captures.json"
            previous = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {}
            merged = {record["file"]: record for record in previous.get("screenshots", [])}
            merged.update({record["file"]: record for record in records})
            (output / "captures.json").write_text(
                json.dumps(
                    {
                        "version": data["version"],
                        "theme": theme_name,
                        "language": language,
                        "screenshots": list(merged.values()),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
        finally:
            win.close()
            wait_until(app, lambda: not win.isVisible())
            manager = win._mod_manager
            manager.stop_ini_sync()
            manager.variants.stop_watch()
            for timer_name in ("_source_state_poll", "_deploy_state_poll", "_watcher_debounce"):
                getattr(win, timer_name).stop()
            pump(app, 500)
            win.deleteLater()
            pump(app, 200)
    print(f"共生成 {len(records)} 张截图：{output}")


def main() -> None:
    """解析主题和场景过滤参数并运行截图。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--theme", choices=("dark", "light"), default="dark")
    parser.add_argument("--only", default="", help="逗号分隔的场景编号，例如 03,07,13")
    parser.add_argument("--language", choices=("zh-CN", "en-US"), default="zh-CN")
    parser.add_argument("--scale", type=int, choices=(1, 2, 3), default=2, help="真实 Qt 渲染倍率，默认 2")
    args = parser.parse_args()
    capture(
        args.theme,
        {part.strip().zfill(2) for part in args.only.split(",") if part.strip()},
        language=args.language,
        scale=args.scale,
    )


if __name__ == "__main__":
    main()
