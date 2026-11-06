"""连接真实插件运行时、旧版目录与宿主资源库，验证完整迁移及重复执行。"""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import zipfile
from pathlib import Path

import pytest
from PIL import Image
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtGui import QDesktopServices

from src.core.mod_package import read_sidecar
from src.ui import theme
from tests.plugins.test_legacy_migration_ui import select_mod
from tests.plugins.test_mainwindow_service import PluginWindow
from tests.plugins.test_operations import library as library

PLUGIN_ID = "official.legacy-migration"


@pytest.fixture
def migration_window(library, temp_data_dir, qtbot, monkeypatch):
    """仅把随包迁移插件复制到隔离目录，再走真实信任和惰性激活流程。"""
    source = Path(__file__).resolve().parents[2] / "plugins" / PLUGIN_ID
    opened_reports = []
    monkeypatch.setattr(QDesktopServices, "openUrl", lambda url: opened_reports.append(url.toLocalFile()) or True)
    shutil.copytree(source, temp_data_dir / "plugins" / PLUGIN_ID)
    window = PluginWindow(library[0])
    window.opened_reports = opened_reports
    qtbot.addWidget(window)
    window.show()
    plugins = window._plugin_manager
    assert plugins.trust(PLUGIN_ID)
    assert plugins.enable(PLUGIN_ID), plugins.get(PLUGIN_ID).error
    yield window
    window.close()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def legacy_collection(tmp_path: Path) -> tuple[Path, str]:
    """按旧项目协议生成无扩展名包、独立预览、分类和一个缺失条目。"""
    root = tmp_path / "legacy-source"
    profile = root / "home/Collection"
    for path in (profile / "modsIndex", profile / "classification", profile / "thumbnail", root / "resources/mods"):
        path.mkdir(parents=True)
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("outfit/mod.ini", "[Constants]\nglobal $outfit = 3\n")
    payload = stream.getvalue()
    sha = hashlib.sha1(payload).hexdigest()
    (root / "resources/mods" / sha).write_bytes(payload)
    (profile / "classification/Outfits").write_text("Unit *\n", encoding="utf-8")
    preview = root / "resources/preview" / (sha + ".png")
    preview.parent.mkdir()
    Image.new("RGB", (24, 24), "blue").save(preview)
    Image.new("RGB", (24, 24), "gold").save(profile / "thumbnail/Unit A.png")
    metadata = {
        "object": "Unit A",
        "type": "zip",
        "name": "Ocean outfit",
        "author": "Sample author",
        "explain": "Preserved collection notes",
        "grading": "P",
        "tags": ["blue", "alternate"],
        "get": [{"url": "https://example.invalid/mod/7", "mode": "get"}],
    }
    missing = {**metadata, "name": "Missing outfit"}
    (profile / "modsIndex/collection.json").write_text(
        json.dumps({"mods": {sha: metadata, "f" * 40: missing}}, ensure_ascii=False), encoding="utf-8"
    )
    return root, sha


def open_and_scan(window, root: Path, qtbot):
    """从工具命令打开真实向导，等后台扫描进入等待确认阶段。"""
    dialog = window._plugin_manager.execute_command(PLUGIN_ID + ".open")
    assert dialog is not None
    instance = window._plugin_manager.get(PLUGIN_ID).instance
    wizard = instance._wizard
    wizard.path_edit.setText(str(root))
    controller = wizard.controller
    controller.discover(str(root))
    qtbot.waitUntil(lambda: controller.state == "selecting", timeout=10000)
    assert len(controller.environments) == 1
    controller.scan_environment(controller.environments[0])
    qtbot.waitUntil(lambda: controller.state == "ready", timeout=10000)
    return dialog, wizard, controller


def test_real_plugin_copies_legacy_metadata_and_reopening_skips_completed_items(
    migration_window, library, tmp_path, qtbot
):
    manager, _ = library
    root, sha = legacy_collection(tmp_path)
    before = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    dialog, wizard, controller = open_and_scan(migration_window, root, qtbot)
    assert len(manager.get_mods()) == 1
    assert controller.summary.pending == 1
    assert any(issue.code == "missing" for issue in controller.issues)
    assert wizard.copy_button.isEnabled()
    original_theme = theme.current_theme(), theme.is_background_active()
    try:
        theme.set_background_active(False)
        dialog.resize(900, 800)
        for mode in ("light", "dark"):
            theme.set_theme(mode)
            migration_window._plugin_ui_host.retheme()
            QCoreApplication.processEvents()
            assert dialog.grab().save(str(tmp_path / f"migration-{mode}.png"))
    finally:
        theme.set_theme(original_theme[0])
        theme.set_background_active(original_theme[1])
        migration_window._plugin_ui_host.retheme()
    wizard.copy_button.click()
    qtbot.waitUntil(lambda: controller.state == "finished", timeout=15000)
    assert controller.summary.succeeded == 1, controller.items
    assert len(migration_window.opened_reports) == 1
    report_path = Path(migration_window.opened_reports[0])
    assert report_path.is_file()
    assert "Ocean outfit" in report_path.read_text(encoding="utf-8-sig")
    assert not controller.summary.failed
    imported = manager.get_mod(controller.items[0].target_id)
    assert imported.name == "Ocean outfit"
    assert imported.info.author == "Sample author"
    assert imported.info.description == "Preserved collection notes"
    assert imported.info.age_rating.value == "P"
    assert imported.info.tags == ["blue", "alternate"]
    assert imported.info.url == "https://example.invalid/mod/7"
    assert not imported.is_enabled
    leaf = manager.get_character(imported.node_id)
    assert leaf.name == "Unit A" and leaf.node_type == "character"
    assert manager.get_character(leaf.parent_id).name == "Outfits"
    assert Path(leaf.thumbnail).is_file()
    assert Path(imported.preview_images[0]).read_bytes() == (root / "resources/preview" / (sha + ".png")).read_bytes()
    assert read_sidecar(imported.path)["info"]["author"] == "Sample author"
    assert before == {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    assert migration_window.refreshes
    dialog.close()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    _dialog, wizard, controller = open_and_scan(migration_window, root, qtbot)
    wizard.copy_button.click()
    qtbot.waitUntil(lambda: controller.state == "finished", timeout=15000)
    assert controller.summary.skipped == 1
    assert controller.items[0].target_id == imported.mod_id
    assert imported.mod_id in Path(controller.report_path).read_text(encoding="utf-8-sig")
    assert len(manager.get_mods()) == 2


def test_real_readable_index_key_imports_with_verified_archive_digest(migration_window, library, tmp_path, qtbot):
    manager, _ = library
    root, sha = legacy_collection(tmp_path)
    key = "角色甲--作者-BuildA"
    (root / "resources/mods" / sha).rename(root / "resources/mods" / key)
    (root / "resources/preview" / (sha + ".png")).rename(root / "resources/preview" / (key + ".png"))
    index = root / "home/Collection/modsIndex/collection.json"
    data = json.loads(index.read_text(encoding="utf-8"))
    data["mods"][key] = data["mods"].pop(sha)
    index.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    before = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    _dialog, wizard, controller = open_and_scan(migration_window, root, qtbot)

    assert controller.summary.pending == 1
    assert controller.items[0].source.legacy_key == key
    assert controller.items[0].source.sha == sha
    wizard.copy_button.click()
    qtbot.waitUntil(lambda: controller.state == "finished", timeout=15000)

    assert controller.summary.succeeded == 1, controller.items
    imported = manager.get_mod(controller.items[0].target_id)
    assert imported.name == "Ocean outfit"
    assert Path(imported.preview_images[0]).read_bytes() == (root / "resources/preview" / (key + ".png")).read_bytes()
    text = Path(controller.report_path).read_text(encoding="utf-8-sig")
    assert "旧版索引键: " + key in text
    assert "原包 SHA-1: " + sha in text
    assert before == {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def test_real_cache_recovery_preserves_edits_and_does_not_reuse_original_history(
    migration_window, library, tmp_path, qtbot
):
    manager, _ = library
    root, sha = legacy_collection(tmp_path)
    cache = root / "home/Collection/work/Mods" / ("disabled-" + sha)
    (cache / "outfit").mkdir(parents=True)
    changed_ini = "[Constants]\nglobal $outfit = 77\n"
    (cache / "outfit/mod.ini").write_text(changed_ini, encoding="utf-8")
    (cache / "preview").mkdir()
    Image.new("RGB", (16, 16), "red").save(cache / "preview/preview_1.webp")
    before = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    dialog, wizard, controller = open_and_scan(migration_window, root, qtbot)
    assert controller.items[0].source.cache_changed
    wizard.copy_button.click()
    qtbot.waitUntil(lambda: controller.state == "finished", timeout=15000)
    assert controller.summary.succeeded == 1
    original_id = controller.items[0].target_id
    dialog.close()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    _dialog, wizard, controller = open_and_scan(migration_window, root, qtbot)
    select_mod(wizard)
    assert wizard.source_combo.isEnabled()
    wizard.source_combo.setCurrentIndex(wizard.source_combo.findData(True))
    assert controller.items[0].source.source_path == str(cache)
    wizard.copy_button.click()
    qtbot.waitUntil(lambda: controller.state == "finished", timeout=15000)
    assert controller.summary.succeeded == 1, controller.items
    recovered = manager.get_mod(controller.items[0].target_id)
    assert recovered.mod_id != original_id
    if Path(recovered.path).is_file():
        with zipfile.ZipFile(recovered.path) as archive:
            name = next(name for name in archive.namelist() if name.endswith("mod.ini"))
            assert archive.read(name).decode().replace("\r\n", "\n") == changed_ini
    else:
        assert next(Path(recovered.path).rglob("mod.ini")).read_text() == changed_ini
    assert not recovered.is_enabled
    assert before == {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    assert len(manager.get_mods()) == 3


def test_real_disabled_index_with_original_archive_is_migrated(migration_window, tmp_path, qtbot):
    root, _sha = legacy_collection(tmp_path)
    index = root / "home/Collection/modsIndex/collection.json"
    index.rename(index.with_name("disabled-collection.json"))
    _dialog, wizard, controller = open_and_scan(migration_window, root, qtbot)
    assert controller.summary.total == 1
    wizard.copy_button.click()
    qtbot.waitUntil(lambda: controller.state == "finished", timeout=15000)
    assert controller.summary.succeeded == 1, controller.items


def test_host_stop_cancels_the_migration_batch_even_before_a_mod_id_exists(migration_window, library, tmp_path, qtbot):
    manager, _ = library
    root, _sha = legacy_collection(tmp_path)
    index_path = root / "home/Collection/modsIndex/collection.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("mod.ini", "[Constants]\nglobal $outfit = 4\n")
    payload = buffer.getvalue()
    second_sha = hashlib.sha1(payload).hexdigest()
    (root / "resources/mods" / second_sha).write_bytes(payload)
    index["mods"][second_sha] = {**next(iter(index["mods"].values())), "name": "Alternate outfit"}
    index_path.write_text(json.dumps(index), encoding="utf-8")
    _dialog, wizard, controller = open_and_scan(migration_window, root, qtbot)
    assert controller.summary.total == 2
    imports_started = []

    def stop_first_import(operation, _ids):
        """模拟用户点击宿主任务面板停止，此时导入尚无目标 Mod 身份。"""
        if operation == "import_mod":
            imports_started.append(operation)
            migration_window._plugin_operations.cancel_all()

    migration_window._plugin_operations.started.connect(stop_first_import)
    wizard.copy_button.click()
    qtbot.waitUntil(lambda: controller.state == "finished", timeout=10000)
    assert imports_started == ["import_mod"]
    assert controller.summary.cancelled == 2
    assert controller.summary.failed == 0
    assert len(manager.get_mods()) == 1
