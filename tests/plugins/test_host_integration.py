from types import SimpleNamespace

import pytest

from src.ui.main_window import MainWindow


@pytest.mark.parametrize(
    "method,args",
    [
        ("_on_overview_mod_toggle", ("mod",)),
        ("_start_async_deploy", ("mod", [])),
        ("_on_overview_batch_disable", (["mod"],)),
        ("_on_overview_mod_delete", ("mod",)),
        ("_on_overview_mod_batch_delete", (["mod"],)),
        ("_on_character_delete", ("character",)),
        ("_on_items_move", ([], ["mod"], "target")),
        ("_on_rebuild_index", ()),
        ("_on_preset_apply", ("preset",)),
        ("_start_mod_update", ("mod",)),
        ("_on_update_all", ()),
        ("_reconcile_deploy_state", ()),
        ("_on_overview_preview_dropped", ("mod", "preview.png")),
        ("_on_overview_preview_delete", ("mod", "preview.png")),
        ("_on_overview_preview_delete_all", ("mod",)),
        ("_on_overview_preview_reorder", ("mod", "preview.png")),
    ],
)
def test_ui_cannot_start_conflicting_work_during_plugin_task(method, args):
    # 不提供下游服务，验证忙碌检查早于准备、对话框和数据修改，包括排队的界面调用。
    window = SimpleNamespace(_plugin_operations=SimpleNamespace(busy=True))
    getattr(MainWindow, method)(window, *args)


@pytest.mark.parametrize(
    "field,value",
    [
        ("_deploy_workers", {"mod": object()}),
        ("_disable_worker", object()),
        ("_preset_apply_worker", object()),
        ("_import_active", True),
        ("_intake_settling", True),
    ],
)
def test_plugin_guard_covers_host_work_until_commit(field, value):
    window = SimpleNamespace(**{field: value})
    assert MainWindow._plugin_host_busy(window)


def test_game_change_during_plugin_work_restores_captured_game():
    reloads = []
    window = SimpleNamespace(
        _plugin_operations=SimpleNamespace(busy=True, game_id="original"),
        _game_service=SimpleNamespace(current_game_id="new"),
        _game_page=SimpleNamespace(reload=lambda: reloads.append(True)),
    )
    MainWindow._on_game_changed(window, "new")
    assert window._game_service.current_game_id == "original"
    assert reloads == [True]


def test_environment_change_is_deferred_during_plugin_io():
    window = SimpleNamespace(_plugin_operations=SimpleNamespace(busy=True))
    MainWindow._apply_migoto_for_current_game(window)
    assert window._deploy_environment_dirty


def test_rejected_plugin_request_does_not_unlock_host_deploy_page():
    enabled = []
    window = SimpleNamespace(
        _deploy_workers={"mod": object()},
        _deploy_active=set(),
        _game_page=SimpleNamespace(setEnabled=enabled.append, setToolTip=lambda _: None),
        _deploy_environment_dirty=False,
        _pending_gb_installs=[],
        _watcher_changed_paths=set(),
    )
    MainWindow._on_plugin_activity_changed(window, False)
    assert enabled == [False]


@pytest.mark.parametrize("plugin_dialog", [False, True])
def test_modal_host_editor_blocks_operations_but_plugin_dialog_allows_them(qtbot, plugin_dialog):
    from PySide6.QtWidgets import QApplication, QDialog

    dialog = QDialog()
    qtbot.addWidget(dialog)
    if plugin_dialog:
        dialog.setObjectName("pluginDialogSurface")
    dialog.setModal(True)
    dialog.show()
    qtbot.waitUntil(lambda: QApplication.activeModalWidget() is dialog)
    try:
        assert MainWindow._plugin_host_busy(SimpleNamespace()) is (not plugin_dialog)
    finally:
        dialog.close()


def test_plugin_dialog_on_top_of_host_modal_does_not_unlock_pending_host_edit(qtbot):
    from PySide6.QtWidgets import QApplication, QDialog

    editor, plugin = QDialog(), QDialog()
    for dialog in (editor, plugin):
        qtbot.addWidget(dialog)
        dialog.setModal(True)
    plugin.setObjectName("pluginDialogSurface")
    editor.show()
    plugin.show()
    qtbot.waitUntil(lambda: QApplication.activeModalWidget() is plugin)
    try:
        assert MainWindow._plugin_host_busy(SimpleNamespace())
    finally:
        plugin.close()
        editor.close()


def test_delayed_intake_is_replanned_after_plugin_work_instead_of_starting_stale_report():
    requests = []
    window = SimpleNamespace(
        _plugin_operations=SimpleNamespace(busy=True),
        _game_service=SimpleNamespace(current_game_id="game", has_active_game=lambda: True),
        _reconcile_library=lambda *args, **kwargs: requests.append((args, kwargs)),
    )
    report = SimpleNamespace(game_id="game")
    assert MainWindow._start_intake_batch(window, report, manual=True) is False
    assert window._plugin_intake_pending == ("game", True)
    MainWindow._resume_plugin_deferred_intake(window)
    assert not requests  # 忙碌状态下保留待发现请求。
    window._plugin_operations.busy = False
    MainWindow._resume_plugin_deferred_intake(window)
    assert requests == [(("game",), {"trigger": "manual", "manual": True})]
    assert window._plugin_intake_pending is None


def test_variant_watch_collection_waits_for_plugin_commit():
    calls = []
    window = SimpleNamespace(
        _plugin_operations=SimpleNamespace(busy=True),
        _preset_apply_worker=None,
        _mod_manager=SimpleNamespace(variants=SimpleNamespace(collect=lambda **kw: calls.append(kw))),
    )
    MainWindow._on_user_ini_changed(window)
    assert not calls and window._plugin_variant_pending
    window._plugin_operations.busy = False
    MainWindow._on_user_ini_changed(window)
    assert calls == [{"trigger": "user_ini_watch", "only_if_changed": True, "event": None}]
    assert not window._plugin_variant_pending


def test_external_archive_seen_during_plugin_work_publishes_without_suppression(qtbot, tmp_path):
    import zipfile

    from PySide6.QtCore import QTimer

    archive = tmp_path / "external.zip"
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr("mod.ini", "[Constants]\nx=1\n")
    observed, published = [], []
    timer = QTimer()
    timer.setSingleShot(True)
    window = SimpleNamespace(
        _plugin_operations=SimpleNamespace(busy=True),
        _import_active=True,
        _file_watcher=SimpleNamespace(_watch_path=tmp_path, generation=1),
        _watcher_changed_paths=set(),
        _watcher_events=[],
        _watcher_debounce=timer,
        _game_service=SimpleNamespace(has_active_game=lambda: True),
        _mod_manager=SimpleNamespace(apply_local_source_events=lambda events: observed.extend(events) or events),
        _publish_local_library_delta=published.append,
    )
    window._on_source_change = lambda event: MainWindow._on_source_change(window, event)
    timer.timeout.connect(lambda: MainWindow._refresh_from_watcher(window))
    try:
        MainWindow._on_watcher_path_event(window, str(archive))
        qtbot.waitUntil(lambda: bool(published), timeout=500)
        assert window._plugin_operations.busy
        assert [event.path for event in observed] == [str(archive)]
        assert not window._watcher_events
    finally:
        timer.stop()
