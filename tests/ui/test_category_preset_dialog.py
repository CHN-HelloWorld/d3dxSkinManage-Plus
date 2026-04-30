from __future__ import annotations

from threading import Event
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QLabel, QPlainTextEdit, QPushButton, QWidget

from tests.core.test_library_reconcile import mm as mm


@pytest.mark.parametrize(
    "source_kind, summary, diagnostic",
    [
        ("schema", "格式不正确", "清单顶层"),
        ("image", "格式不正确", "目录图片损坏"),
        ("missing", "可以访问", "无法读取分类预设文件"),
    ],
)
def test_source_errors_keep_the_correct_recovery_guidance_and_expandable_details(
    qtbot,
    mm,
    tmp_path,
    source_kind,
    summary,
    diagnostic,
):
    import hashlib

    from src.ui.dialogs.category_preset import CategoryPresetDialog
    from tests.services.test_category_preset_file import raw_file

    source = tmp_path / "source.d3dxcp"
    if source_kind == "schema":
        raw_file(source, raw_manifest=b"[]")
    elif source_kind == "image":
        payload = b"not an image"
        digest = hashlib.sha256(payload).hexdigest()
        raw_file(
            source,
            {
                "kind": "d3dxSkinManage-Plus.category-preset",
                "nodes": [{"path": "a", "node_type": "category", "image_id": digest}],
                "images": [{"id": digest, "offset": 0, "length": len(payload), "media_type": "image/png"}],
            },
            images=payload,
        )
    dialog = CategoryPresetDialog(mm, "game_a")
    qtbot.addWidget(dialog)
    dialog.show()
    dialog._load_source(source)
    qtbot.waitUntil(lambda: not dialog.busy)

    messages = "\n".join(widget.text() for widget in dialog._page.findChildren(QLabel))
    assert summary in messages
    details = dialog._page.findChild(QPlainTextEdit)
    assert not details.isVisible()
    toggle = next(b for b in dialog._page.findChildren(QPushButton) if b.text() == "详细信息")
    toggle.click()
    assert details.isVisible() and diagnostic in details.toPlainText()


@pytest.mark.parametrize("as_exception", [False, True])
def test_actionable_source_errors_are_not_replaced_by_format_errors(qtbot, mm, as_exception):
    from src.core.category_preset import CategoryPresetError
    from src.ui.dialogs.category_preset import CategoryPresetDialog

    message = "目标游戏已被删除，请重新选择" if as_exception else "请一次选择一个分类预设文件（.d3dxcp）。"
    error = CategoryPresetError(message) if as_exception else message
    dialog = CategoryPresetDialog(mm, "game_a")
    qtbot.addWidget(dialog)
    dialog._show_error(error, file_error=True)
    assert message in "\n".join(widget.text() for widget in dialog._page.findChildren(QLabel))


def row(path, *, name="", node_type="category", status="new", **values):
    fields = dict(
        path=path,
        name=name or path.rsplit("/", 1)[-1],
        name_en="",
        node_type=node_type,
        status=status,
        image_path="",
        has_image=False,
        fill_image=False,
        detail="",
        local_name="",
        local_node_type="",
        local_image_path="",
    )
    fields.update(values)
    return SimpleNamespace(**fields)


def test_tree_search_keeps_ancestors_and_does_not_change_import_scope(qtbot):
    from src.ui.dialogs.category_preset_widgets import PresetTree

    tree = PresetTree()
    qtbot.addWidget(tree)
    tree.set_rows(
        (
            row("角色"),
            row("角色/类别甲", name="类别甲"),
            row("角色/类别甲/示例角色壬", node_type="character", name_en="Iota"),
            row("武器"),
        )
    )
    tree.search.setText("iota")
    assert {path for path, item in tree.items.items() if not item.isHidden()} == {
        "角色",
        "角色/类别甲",
        "角色/类别甲/示例角色壬",
    }
    assert len(tree.rows) == 4
    tree.search.clear()
    assert all(not item.isHidden() for item in tree.items.values())


def test_tree_uses_windows_path_identity_for_parent_links_and_search(qtbot):
    from src.ui.dialogs.category_preset_widgets import PresetTree

    tree = PresetTree()
    qtbot.addWidget(tree)
    tree.set_rows((row("Root"), row("ROOT/Child")))
    assert tree.tree.topLevelItemCount() == 1
    assert tree.items["ROOT/Child"].parent() is tree.items["Root"]
    tree.search.setText("child")
    assert not tree.items["Root"].isHidden()


def test_conflict_navigation_clears_search_and_selects_actual_path(qtbot):
    from src.ui.dialogs.category_preset_widgets import PresetTree

    tree = PresetTree()
    qtbot.addWidget(tree)
    tree.set_rows((row("武器"), row("武器/静水", status="conflict", detail="该位置已有同名 Mod"), row("角色")))
    tree.search.setText("角色")
    tree.tree.collapseAll()
    tree.next_issue()
    assert tree.search.text() == ""
    assert tree.tree.currentItem().data(0, Qt.ItemDataRole.UserRole).path == "武器/静水"
    assert tree.items["武器"].isExpanded()


def test_tree_preserves_existing_names_and_marks_image_supplements_separately(qtbot):
    from src.ui.dialogs.category_preset_widgets import PresetTree

    tree = PresetTree()
    qtbot.addWidget(tree)
    tree.set_rows(
        (row("角色", name="包内名称", status="existing", local_name="本地名称", has_image=True, fill_image=True),)
    )
    item = tree.items["角色"]
    assert item.text(0) == "本地名称"
    assert item.text(2) == "已存在"
    assert item.text(3) == "补充目录图片"
    assert not item.flags() & Qt.ItemFlag.ItemIsUserCheckable


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("示例游戏甲", "示例游戏甲.d3dxcp"),
        ("示例游戏甲.D3DXCP", "示例游戏甲.d3dxcp"),
        ("示例游戏甲.d3dxcp.d3dxcp", "示例游戏甲.d3dxcp"),
    ],
)
def test_export_name_has_one_normalized_extension(name, expected):
    from src.ui.dialogs.category_preset_widgets import preset_filename, preset_name_error

    assert not preset_name_error(name)
    assert preset_filename(name) == expected


@pytest.mark.parametrize(
    "name",
    ["", " ", ".d3dxcp", "../share", "C:share", "CON", "share.", "share ", "CONIN$", "CONOUT$", "COM¹", "😀" * 125],
)
def test_export_rejects_names_that_cannot_be_saved_on_windows(name):
    from src.ui.dialogs.category_preset_widgets import preset_name_error

    assert preset_name_error(name)


def test_export_filename_accepts_the_valid_utf16_boundary():
    from src.ui.dialogs.category_preset_widgets import preset_filename, preset_name_error

    name = "😀" * 124
    assert not preset_name_error(name)
    assert len(preset_filename(name).encode("utf-16-le")) // 2 == 255


def test_committed_import_with_pending_cleanup_shows_paths_without_plain_success(qtbot, mm, tmp_path):
    from src.ui.dialogs.category_preset import CategoryPresetDialog

    dialog = CategoryPresetDialog(mm, "game_a")
    qtbot.addWidget(dialog)
    completed = []
    dialog.import_completed.connect(completed.append)
    pending = str(tmp_path / "changed-file")
    result = SimpleNamespace(
        game_id="game_a", new_count=1, existing_count=0, fill_count=0, new_image_count=0, recovery_paths=(pending,)
    )
    dialog._import_success(result)
    messages = [widget.text() for widget in dialog.findChildren(QLabel)]
    assert "导入完成" not in messages
    assert any("待" in message and "清理" in message for message in messages)
    assert any(pending in message for message in messages)
    assert completed == ["game_a"]


def import_plan(mm, tmp_path, *, new=1, existing=0, fill=0, conflicts=0):
    return SimpleNamespace(
        target=SimpleNamespace(
            game_id="game_a", name="示例游戏甲", name_en="ExampleGameA", root=mm._index.paths.roots["game_a"]
        ),
        source_name="分类树.d3dxcp",
        preset_name="分类树",
        suggested_game_name="示例游戏甲",
        rows=(
            row(
                "角色",
                status="conflict" if conflicts else "new" if new else "existing",
                has_image=bool(fill),
                fill_image=bool(fill),
            ),
        ),
        new_count=new,
        existing_count=existing,
        fill_count=fill,
        conflict_count=conflicts,
        new_image_count=0,
        can_import=not conflicts and bool(new or fill),
    )


@pytest.mark.parametrize(
    ("counts", "enabled", "copy"),
    [
        ({"new": 0, "existing": 1}, False, "全部目录已存在，无需导入"),
        ({"new": 0, "existing": 1, "fill": 1}, True, "补充"),
        ({"new": 1, "conflicts": 1}, False, "路径冲突"),
    ],
)
def test_import_preview_respects_complete_plan_and_fixed_target(qtbot, mm, tmp_path, counts, enabled, copy):
    from src.ui.dialogs.category_preset import CategoryPresetDialog

    dialog = CategoryPresetDialog(mm, "game_a")
    qtbot.addWidget(dialog)
    dialog.show_import_plan(import_plan(mm, tmp_path, **counts))
    start = dialog.findChild(QPushButton, "categoryPresetStartImport")
    assert start.isEnabled() is enabled
    text = "\n".join(widget.text() for widget in dialog.findChildren(QLabel))
    assert copy in text
    assert str(mm._index.paths.roots["game_a"]) in text
    assert "分类根目录" in text


def test_missing_game_offers_navigation_without_import_or_export(qtbot, mm):
    from src.ui.dialogs.category_preset import CategoryPresetDialog

    dialog = CategoryPresetDialog(mm, "")
    qtbot.addWidget(dialog)
    assert "请先添加游戏" in dialog.windowTitle()
    assert dialog.findChild(QPushButton, "categoryPresetStartImport") is None
    go = dialog.findChild(QPushButton, "categoryPresetGoGames")
    with qtbot.waitSignal(dialog.games_requested):
        go.click()


def test_export_filename_validation_updates_the_actual_submit_button(qtbot, mm, tmp_path):
    from src.ui.dialogs.category_preset import CategoryPresetDialog

    dialog = CategoryPresetDialog(mm, "game_a")
    qtbot.addWidget(dialog)
    dialog._destination = str(tmp_path)
    plan = SimpleNamespace(
        target=import_plan(mm, tmp_path).target,
        rows=(row("角色"),),
        image_count=0,
        image_bytes=0,
        image_issues={},
        include_images=True,
        omitted_images=(),
    )
    dialog.show_export_plan(plan)
    assert dialog.findChild(QPushButton, "categoryPresetStartExport").isEnabled()
    dialog._name_input.setText("CON")
    assert not dialog.findChild(QPushButton, "categoryPresetStartExport").isEnabled()
    dialog._name_input.setText("示例游戏甲.D3DXCP")
    assert dialog._filename_label.text() == "示例游戏甲.d3dxcp"
    assert dialog.findChild(QPushButton, "categoryPresetStartExport").isEnabled()


@pytest.mark.parametrize("mode", ["source", "preview", "export"])
def test_preset_pages_center_on_application_window_after_resizing(qtbot, mm, tmp_path, mode):
    from src.ui.dialogs.category_preset import CategoryPresetDialog

    host = QWidget()
    qtbot.addWidget(host)
    host.setWindowFlag(Qt.WindowType.FramelessWindowHint)
    available = host.screen().availableGeometry()
    host.setGeometry(available.adjusted(24, 24, -4, -4))
    page = QWidget(host)
    page.setGeometry(25, 60, 500, 450)
    host.show()
    dialog = CategoryPresetDialog(mm, "game_a", page, start_import=True)
    dialog.show()
    qtbot.wait(20)
    if mode != "source":
        # 模拟移动较小的源窗口后选择文件。
        dialog.move(80, 90)
        host.move(available.topLeft() + QPoint(20, 20))
        plan = import_plan(mm, tmp_path)
        if mode == "preview":
            dialog.show_import_plan(plan)
        else:
            dialog.show_export_plan(
                SimpleNamespace(
                    target=plan.target,
                    rows=(row("角色"),),
                    image_count=0,
                    image_bytes=0,
                    image_issues={},
                    include_images=True,
                    omitted_images=(),
                )
            )
        qtbot.wait(20)
    offset = dialog.frameGeometry().center() - host.frameGeometry().center()
    assert abs(offset.x()) <= 1 and abs(offset.y()) <= 1
    dialog.close()


def test_closing_waits_for_background_cancellation_before_destroying_dialog(qtbot, mm):
    from src.core.category_preset import CategoryPresetCancelled
    from src.ui.dialogs.category_preset import CategoryPresetDialog

    started, allow_finish = Event(), Event()
    dialog = CategoryPresetDialog(mm, "game_a")
    qtbot.addWidget(dialog)
    dialog.show()

    def work(*, cancelled, progress):
        started.set()
        allow_finish.wait(5)
        if cancelled():
            raise CategoryPresetCancelled()
        return "unexpected success"

    try:
        dialog._run_task("read", work, lambda result: None)
        qtbot.waitUntil(started.is_set)
        dialog.close()
        assert dialog.isVisible() and dialog.busy
        allow_finish.set()
        qtbot.waitUntil(lambda: not dialog.busy and not dialog.isVisible())
    finally:
        allow_finish.set()
        qtbot.waitUntil(lambda: not dialog.busy)


def test_committed_task_cannot_be_cancelled_or_discard_its_success(qtbot, mm):
    from src.ui.dialogs.category_preset import CategoryPresetDialog

    allow_finish = Event()
    delivered = []
    dialog = CategoryPresetDialog(mm, "game_a")
    qtbot.addWidget(dialog)
    dialog.show()

    def work(*, cancelled, progress):
        progress("已提交", 96)
        allow_finish.wait(5)
        return not cancelled()

    try:
        dialog._run_task("import", work, delivered.append)
        qtbot.waitUntil(lambda: dialog._committed)
        assert not dialog._cancel_button.isEnabled()
        dialog.close()
        assert dialog.isVisible()
        allow_finish.set()
        qtbot.waitUntil(lambda: not dialog.busy and not dialog.isVisible())
        assert delivered == [True]
    finally:
        allow_finish.set()
        qtbot.waitUntil(lambda: not dialog.busy)


def test_progress_caption_alone_does_not_disable_cancellation(qtbot, mm):
    from src.ui.dialogs.category_preset import CategoryPresetDialog

    allow_finish = Event()
    dialog = CategoryPresetDialog(mm, "game_a")
    qtbot.addWidget(dialog)

    def work(*, cancelled, progress):
        progress("正在完成…", 95)
        allow_finish.wait(5)

    try:
        dialog._run_task("import", work, lambda result: None)
        qtbot.waitUntil(lambda: dialog._progress_bar.value() == 95)
        assert not dialog._committed
        assert dialog._cancel_button.isEnabled()
        dialog._cancel_task()
        assert dialog._worker.cancel_event.is_set()
    finally:
        allow_finish.set()
        qtbot.waitUntil(lambda: not dialog.busy)


def test_cancel_export_collection_only_offers_close(qtbot, mm):
    from src.core.category_preset import CategoryPresetCancelled
    from src.ui.dialogs.category_preset import CategoryPresetDialog

    started = Event()
    dialog = CategoryPresetDialog(mm, "game_a")
    qtbot.addWidget(dialog)
    dialog.show()

    def collect(*, cancelled, progress):
        started.set()
        while not cancelled():
            Event().wait(0.01)
        raise CategoryPresetCancelled()

    dialog._run_task("export_preview", collect, dialog.show_export_plan)
    qtbot.waitUntil(started.is_set)
    dialog._cancel_button.click()
    qtbot.waitUntil(lambda: not dialog.busy)
    qtbot.waitUntil(lambda: dialog._page.isVisible())
    # 等待后插入页脚的 Show 事件，父控件可见不等于页脚已可绘制。
    qtbot.waitUntil(lambda: any(b.isVisible() for b in dialog._page.findChildren(QPushButton)))
    assert [b.text() for b in dialog._page.findChildren(QPushButton) if b.isVisible()] == ["关闭"]


@pytest.mark.parametrize("kind", ["import", "export"])
def test_close_keeps_preview_until_silent_cleanup_finishes(qtbot, mm, kind):
    from src.ui.dialogs.category_preset import CategoryPresetDialog

    started, finish = Event(), Event()
    dialog = CategoryPresetDialog(mm, "game_a")
    qtbot.addWidget(dialog)
    dialog.show()
    plan = SimpleNamespace(staging="temporary")
    setattr(dialog, f"_{kind}_plan", plan)

    def discard(plan):
        started.set()
        finish.wait(5)

    dialog._service_instance = SimpleNamespace(discard=discard)
    original_page, original_size, original_title = dialog._page, dialog.size(), dialog.windowTitle()
    try:
        dialog.reject()
        qtbot.waitUntil(started.is_set)
        assert dialog._page is original_page
        assert dialog.size() == original_size
        assert dialog.windowTitle() == original_title
    finally:
        finish.set()
        qtbot.waitUntil(lambda: not dialog.busy and not dialog.isVisible())
