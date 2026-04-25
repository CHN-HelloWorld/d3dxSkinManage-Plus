from __future__ import annotations

import json
from threading import Event

import pytest
from PIL import Image
from PySide6.QtWidgets import QLabel, QPushButton

from src.core.category_preset import CategoryPresetDocument, CategoryPresetNode
from src.services.category_preset_file import read_category_preset, write_category_preset
from tests.core.test_library_reconcile import _import_mod
from tests.core.test_library_reconcile import mm as mm


def wait_page(qtbot, dialog, mode):
    qtbot.waitUntil(lambda: not dialog.busy, timeout=15000)
    assert dialog._mode == mode, "\n".join(label.text() for label in dialog.findChildren(QLabel))


@pytest.fixture
def dialogs(qtbot, mm):
    from src.ui.dialogs.category_preset import CategoryPresetDialog

    opened = []

    def create(game_id="game_a", **kwargs):
        dialog = CategoryPresetDialog(mm, game_id, **kwargs)
        # 窗口须先完成异步文件清理，再由 pytest-qt 安排延迟销毁。
        opened.append(dialog)
        dialog.show()
        return dialog

    yield create
    for dialog in reversed(opened):
        dialog.reject()
        qtbot.waitUntil(lambda: not dialog.busy, timeout=15000)
        dialog.deleteLater()


def test_direct_export_prepares_captured_game_without_writing(qtbot, mm, tmp_path, dialogs):
    root = mm.add_character("角色")
    mm.add_character("示例角色壬", parent_id=root.id)
    dialog = dialogs(start_export=True)
    mm.set_game("game_b")
    wait_page(qtbot, dialog, "export")
    assert dialog.game_id == "game_a"
    assert dialog._export_plan.target.name == "game_a"
    assert len(dialog._export_plan.rows) == 2
    assert not dialog.findChild(QPushButton, "categoryPresetStartExport").isEnabled()
    assert not list(tmp_path.rglob("*.d3dxcp"))


@pytest.mark.parametrize("game_id", ["", "game_a"])
def test_direct_export_keeps_missing_game_and_empty_category_guidance(qtbot, dialogs, game_id):
    dialog = dialogs(game_id, start_export=True)
    assert not dialog.busy
    assert dialog._export_plan is None
    labels = "\n".join(label.text() for label in dialog.findChildren(QLabel))
    assert ("暂无可导出的分类" if game_id else "游戏") in labels
    if game_id:
        export = next(
            button for button in dialog.findChildren(QPushButton) if button.accessibleName() == "导出分类预设"
        )
        assert not export.isEnabled()


def test_native_export_then_import_another_game_preserves_tree_images_and_mods(qtbot, mm, tmp_path, dialogs):
    parent = mm.add_character("角色")
    group = mm.add_character("类别甲", parent_id=parent.id)
    leaf = mm.add_character("示例角色壬", name_en="Iota", node_type="character", parent_id=group.id)
    empty = mm.add_character("空目录")
    art = tmp_path / "portrait.png"
    Image.new("RGB", (40, 24), "#537b59").save(art)
    assert mm.update_character_thumbnail(leaf.id, str(art))
    source_mod = _import_mod(mm, leaf.id, tmp_path, "source-mod")
    source_rows = [tuple(row) for row in mm._index._db().execute("SELECT * FROM nodes WHERE game_id='game_a'")]

    exporter = dialogs()
    exporter._destination = str(tmp_path)
    exporter._prepare_export()
    wait_page(qtbot, exporter, "export")
    assert len(exporter._export_plan.rows) == 4
    assert exporter._export_plan.image_count == 1
    exporter.findChild(QPushButton, "categoryPresetStartExport").click()
    wait_page(qtbot, exporter, "export_result")
    output = next(tmp_path.glob("*.d3dxcp"))
    document = read_category_preset(output)
    assert {node.path for node in document.nodes} == {"角色", "角色/类别甲", "角色/类别甲/示例角色壬", "空目录"}
    exporter.reject()
    qtbot.waitUntil(lambda: not exporter.busy)

    mm.set_game("game_b")
    local = mm.add_character("本地自定义")
    local_mod = _import_mod(mm, local.id, tmp_path, "local-mod")
    mods_before = [tuple(row) for row in mm._index._db().execute("SELECT * FROM mods ORDER BY mod_id")]
    imported = []
    importer = dialogs("game_b", start_import=True)
    importer.import_completed.connect(imported.append)
    importer._load_source(output)
    wait_page(qtbot, importer, "preview")
    assert (importer._import_plan.new_count, importer._import_plan.new_image_count) == (4, 1)
    assert "当前为“game_b”" in "\n".join(label.text() for label in importer.findChildren(QLabel))
    # 其他位置发生游戏切换不能改变已捕获的导入目标。
    mm.set_game("game_a")
    importer.findChild(QPushButton, "categoryPresetStartImport").click()
    wait_page(qtbot, importer, "result")
    assert imported == ["game_b"]
    target_rows = list(mm._index._db().execute("SELECT * FROM nodes WHERE game_id='game_b'"))
    assert len(target_rows) == 5
    assert {row["id"] for row in target_rows}.isdisjoint({parent.id, group.id, leaf.id, empty.id})
    target_leaf = next(row for row in target_rows if row["name_en"] == "Iota")
    assert target_leaf["node_type"] == "character"
    image_path = mm._index.paths.resolve_image(target_leaf["id"], json.loads(target_leaf["thumbnail"]))
    assert image_path.read_bytes() == art.read_bytes()
    assert mods_before == [tuple(row) for row in mm._index._db().execute("SELECT * FROM mods ORDER BY mod_id")]
    assert mm.get_mod(source_mod.mod_id) and mm.get_mod(local_mod.mod_id)
    assert source_rows == [tuple(row) for row in mm._index._db().execute("SELECT * FROM nodes WHERE game_id='game_a'")]
    assert mm._index._db().execute("SELECT COUNT(*) FROM category_import_journal").fetchone()[0] == 0

    # 即使发送者副本变化，结果操作仍持有已校验文件。
    output.write_bytes(b"original changed after import")
    importer._recompare()
    wait_page(qtbot, importer, "preview")
    assert (importer._import_plan.new_count, importer._import_plan.existing_count) == (0, 4)
    assert not importer.findChild(QPushButton, "categoryPresetStartImport").isEnabled()
    assert imported == ["game_b"]


def test_import_into_game_created_after_startup_refreshes_real_overview(qtbot, mm, tmp_path, dialogs):
    import hashlib
    from pathlib import Path
    from types import MethodType, SimpleNamespace

    from src.core.game_service import GameConfig
    from src.ui.main_window import MainWindow
    from src.ui.pages.overview import OverviewPage

    # 先打开索引，再创建并启用游戏，最后导入含图片的预设，模拟实际操作顺序。
    game_id = mm._game_service.save_game(GameConfig(name="522"))
    mm.set_game(game_id)
    art = tmp_path / "new-game.png"
    Image.new("RGB", (30, 20), "green").save(art)
    image_id = hashlib.sha256(art.read_bytes()).hexdigest()
    source = tmp_path / "new-game.d3dxcp"
    write_category_preset(
        source,
        CategoryPresetDocument(
            "预设",
            (
                CategoryPresetNode("角色", "角色", "category", image_id=image_id),
                CategoryPresetNode("角色/子目录", "子目录", "character"),
            ),
        ),
        {image_id: art},
    )
    dialog = dialogs(game_id, start_import=True)
    dialog._load_source(source)
    wait_page(qtbot, dialog, "preview")
    dialog.findChild(QPushButton, "categoryPresetStartImport").click()
    wait_page(qtbot, dialog, "result")
    assert mm._index._db().execute("SELECT COUNT(*) FROM nodes WHERE game_id=?", (game_id,)).fetchone()[0] == 2

    page = OverviewPage()
    qtbot.addWidget(page)
    page.set_current_game(game_id)
    page.show()
    refreshed = []

    def refresh_mods():
        refreshed.append(game_id)
        page.set_current_mods([], [])

    window = SimpleNamespace(
        _mod_manager=mm,
        _game_service=mm._game_service,
        _library_probe=None,
        _overview_page=page,
        _refresh_overview_mods=refresh_mods,
    )
    window._reload_overview_level = MethodType(MainWindow._reload_overview_level, window)
    try:
        # 验证成功提示后的回调能够读取有效记录。
        MainWindow._on_category_preset_imported(window, game_id)
        assert refreshed == [game_id]
        assert {node["name"] for node in page._all_characters} == {"角色", "子目录"}
        assert [item["data"]["name"] for item in page._display_items] == ["角色"]
        thumbnail = Path(page._display_items[0]["data"]["thumbnail"])
        assert thumbnail.read_bytes() == art.read_bytes()
        # 周期后台扫描使用相同的游戏映射。
        list(mm.iter_library_scan_layout(game_id))
    finally:
        page.close()


def test_export_missing_image_requires_explicit_omission_but_keeps_category(qtbot, mm, tmp_path, dialogs):
    node = mm.add_character("缺图目录")
    art = tmp_path / "cover.png"
    Image.new("RGB", (4, 4), "blue").save(art)
    assert mm.update_character_thumbnail(node.id, str(art))
    thumbnail = mm._index._db().execute("SELECT thumbnail FROM nodes WHERE id=?", (node.id,)).fetchone()[0]
    mm._index.paths.resolve_image(node.id, json.loads(thumbnail)).unlink()
    dialog = dialogs()
    dialog._destination = str(tmp_path)
    dialog._prepare_export()
    wait_page(qtbot, dialog, "export")
    assert list(dialog._export_plan.image_issues) == ["缺图目录"]
    assert not dialog.findChild(QPushButton, "categoryPresetStartExport").isEnabled()
    dialog.findChild(QPushButton, "categoryPresetOmitImage").click()
    wait_page(qtbot, dialog, "export")
    assert len(dialog._export_plan.rows) == 1 and not dialog._export_plan.image_issues
    dialog.findChild(QPushButton, "categoryPresetStartExport").click()
    wait_page(qtbot, dialog, "export_result")
    document = read_category_preset(next(tmp_path.glob("*.d3dxcp")))
    assert [node.path for node in document.nodes] == ["缺图目录"]
    assert not document.images and not document.nodes[0].image_id
    assert mm._index._db().execute("SELECT thumbnail FROM nodes WHERE id=?", (node.id,)).fetchone()[0] == thumbnail


@pytest.mark.parametrize("kind", ["zip", "json", "directory", "renamed"])
def test_wrong_input_shows_only_file_error_and_leaves_library_untouched(qtbot, mm, tmp_path, dialogs, kind):
    path = tmp_path / ("bad.d3dxcp" if kind == "renamed" else f"bad.{kind}")
    if kind == "directory":
        path.mkdir()
    else:
        path.write_bytes(b"PK not a category preset")
    dialog = dialogs(start_import=True)
    dialog._load_source(path)
    wait_page(qtbot, dialog, "file_error")
    assert dialog._import_plan is None
    assert not dialog.findChildren(QPushButton, "categoryPresetStartImport")
    assert mm._index._db().execute("SELECT COUNT(*) FROM nodes").fetchone()[0] == 0
    assert mm._index._db().execute("SELECT COUNT(*) FROM mods").fetchone()[0] == 0


def test_conflict_recompare_uses_fixed_source_then_imports_complete_tree(qtbot, mm, tmp_path, dialogs):
    source = tmp_path / "fixed.d3dxcp"
    write_category_preset(
        source,
        CategoryPresetDocument(
            "预设",
            (
                CategoryPresetNode("角色", "角色", "category"),
                CategoryPresetNode("角色/子目录", "子目录", "character"),
            ),
        ),
        {},
    )
    occupant = mm._get_library_root("game_a") / "角色"
    occupant.write_bytes(b"local file")
    dialog = dialogs(start_import=True)
    dialog._load_source(source)
    wait_page(qtbot, dialog, "preview")
    assert dialog._import_plan.conflict_count and not dialog._import_plan.can_import
    assert not dialog.findChild(QPushButton, "categoryPresetStartImport").isEnabled()
    source.write_bytes(b"sender changed original")
    occupant.unlink()
    dialog._recompare()
    wait_page(qtbot, dialog, "preview")
    assert dialog._import_plan.new_count == 2 and dialog._import_plan.can_import
    dialog.findChild(QPushButton, "categoryPresetStartImport").click()
    wait_page(qtbot, dialog, "result")
    assert (occupant / "子目录").is_dir()
    assert mm._index._db().execute("SELECT COUNT(*) FROM nodes").fetchone()[0] == 2


def test_close_waits_for_preview_image_reader_before_removing_its_file(qtbot, mm, tmp_path, dialogs, monkeypatch):
    import hashlib

    from src.ui.dialogs import category_preset_widgets as widgets

    art = tmp_path / "image.png"
    Image.new("RGB", (30, 20), "green").save(art)
    image_id = hashlib.sha256(art.read_bytes()).hexdigest()
    source = tmp_path / "preview.d3dxcp"
    write_category_preset(
        source,
        CategoryPresetDocument("预设", (CategoryPresetNode("带图目录", "带图目录", "category", image_id=image_id),)),
        {image_id: art},
    )
    reading, allow_read, cleaning = Event(), Event(), Event()
    real_reader = widgets.QImageReader

    class SlowReader:
        def __init__(self, path):
            self.reader = real_reader(path)

        def __getattr__(self, name):
            return getattr(self.reader, name)

        def read(self):
            reading.set()
            assert allow_read.wait(5)
            return self.reader.read()

    monkeypatch.setattr(widgets, "QImageReader", SlowReader)
    dialog = dialogs(start_import=True)
    try:
        dialog._load_source(source)
        wait_page(qtbot, dialog, "preview")
        qtbot.waitUntil(reading.is_set)
        stage = dialog._import_plan.staging
        discard = dialog._service.discard

        def record_cleanup(plan):
            cleaning.set()
            return discard(plan)

        monkeypatch.setattr(dialog._service, "discard", record_cleanup)
        dialog.close()
        qtbot.wait(30)
        assert dialog.busy and dialog.isVisible()
        assert not cleaning.is_set(), "cleanup raced the preview decoder's file handle"
        allow_read.set()
        qtbot.waitUntil(lambda: not dialog.busy and not dialog.isVisible())
        assert cleaning.is_set() and not stage.exists()
    finally:
        allow_read.set()


@pytest.mark.parametrize("action", ["close", "replace_source", "refresh_export"])
def test_transient_cleanup_failure_keeps_plan_owned_for_retry(qtbot, mm, tmp_path, dialogs, monkeypatch, action):
    source = tmp_path / "source.d3dxcp"
    write_category_preset(source, CategoryPresetDocument("预设", (CategoryPresetNode("目录", "目录", "category"),)), {})
    dialog = dialogs()
    if action == "refresh_export":
        mm.add_character("目录")
        operation = dialog._prepare_export
        operation()
        wait_page(qtbot, dialog, "export")
        stage = dialog._export_plan.staging
    else:
        dialog._load_source(source)
        wait_page(qtbot, dialog, "preview")
        stage = dialog._import_plan.staging
        operation = dialog.reject if action == "close" else lambda: dialog._load_source(source)
    discard = dialog._service.discard
    calls = []

    def fail_once(plan):
        calls.append(plan.staging)
        if len(calls) == 1:
            raise PermissionError(f"暂时占用：{stage}")
        return discard(plan)

    monkeypatch.setattr(dialog._service, "discard", fail_once)
    operation()
    qtbot.waitUntil(lambda: not dialog.busy)
    assert stage.exists()
    operation()
    qtbot.waitUntil(lambda: not dialog.busy)
    assert calls[:2] == [stage, stage]
    assert not stage.exists()
    assert (not dialog.isVisible()) if action == "close" else dialog._mode in {"preview", "export"}
