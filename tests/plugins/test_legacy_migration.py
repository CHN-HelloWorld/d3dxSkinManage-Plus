"""验证旧版迁移向导的任务顺序、重复识别、取消边界与窗口生命周期。"""

from __future__ import annotations

import importlib
import sys
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Event, get_ident
from types import ModuleType, SimpleNamespace
from uuid import uuid4

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QTimer

from src.plugin_sdk import (
    BackgroundTask,
    DisposableStore,
    OperationResult,
    OperationTask,
    TaskCancelled,
    TaskContext,
    TaskResult,
)
from src.plugin_sdk.facades import ModInfo
from src.plugin_sdk.i18n import TranslationFacade


@pytest.fixture
def load_migration():
    """使用独立包名加载实际分发文件，并在测试后移除包及其子模块。"""
    directory = Path(__file__).resolve().parents[2] / "plugins" / "official.legacy-migration"
    name = "test_legacy_migration_" + uuid4().hex
    package = ModuleType(name)
    package.__path__ = [str(directory)]
    sys.modules[name] = package

    def load(module):
        """让缺失实现以明确断言失败，而不是测试收集异常。"""
        assert (directory / (module + ".py")).is_file(), "旧版迁移插件尚未实现"
        return importlib.import_module(name + "." + module)

    yield load
    for key in tuple(sys.modules):
        if key == name or key.startswith(name + "."):
            sys.modules.pop(key)


class MemoryStorage:
    """保留 JSON 形状的存储替身，供控制器验证迁移历史。"""

    def __init__(self):
        """创建测试独有的数据容器。"""
        self.values = {}

    def get(self, key, default=None):
        """读取已有值，缺失时沿用 SDK 默认值语义。"""
        return self.values.get(key, default)

    def set(self, key, value):
        """记录控制器实际持久化的结果。"""
        self.values[key] = value


class ControlledTasks:
    """使用真实后台任务对象，在测试指定的时刻交付工作结果。"""

    def __init__(self):
        """分别保存尚未完成任务和取消信号。"""
        self.calls = []

    def run(self, worker, *, title=""):
        """保留工作函数，测试可以独立验证线程与取消契约。"""
        cancelled = Event()
        task = BackgroundTask(cancel=cancelled.set, title=title)
        self.calls.append(SimpleNamespace(worker=worker, task=task, cancelled=cancelled))
        return task

    def finish(self, value):
        """在当前宿主线程完成最近一次后台任务。"""
        self.calls[-1].task._finish(TaskResult(value=value))


class ControlledLibrary:
    """记录宿主线程上的 SDK 调用，并用真实业务任务控制完成顺序。"""

    def __init__(self):
        """默认同步完成，个别场景可切换为手动完成。"""
        self.thread = get_ident()
        self.game_id = "game-a"
        self.reads = 0
        self.categories = []
        self.imports = []
        self.tasks = []
        self.catalog = {}
        self.immediate = True
        self.defer_imports = False
        self.responses = deque()
        self.cancelled = []

    def current_game_id(self):
        """所有数据门面调用都必须发生在创建测试库的线程。"""
        assert get_ident() == self.thread
        self.reads += 1
        return self.game_id

    def current_game_name(self):
        """返回与游戏身份分离的可读名称。"""
        assert get_ident() == self.thread
        return "示例游戏" if self.game_id else ""

    def get_mod(self, mod_id):
        """按身份查询真实形状的 SDK 快照。"""
        assert get_ident() == self.thread
        return self.catalog.get(mod_id)

    def create_category(self, name, **options):
        """将分类请求写入边界记录，并返回同步或待完成任务。"""
        assert get_ident() == self.thread
        self.categories.append((name, options))
        result = OperationResult(self.game_id, node_id="node-" + str(len(self.categories)))
        return self._task(result)

    def import_mod(self, request):
        """记录完整导入请求，模拟成功提交后可查询的 Mod 身份。"""
        assert get_ident() == self.thread
        self.imports.append(request)
        target_id = "mod-" + str(len(self.imports))
        self.catalog[target_id] = ModInfo(target_id, request.name, node_id=request.node_id, game_id=self.game_id)
        result = self.responses.popleft() if self.responses else OperationResult(self.game_id, (target_id,))
        return self._task(result, defer=self.defer_imports)

    def _task(self, result, *, defer=False):
        """让同步完成路径也经过 SDK 的回调机制。"""
        task = OperationTask(cancel=lambda: self.cancelled.append(task))
        self.tasks.append(task)
        if self.immediate and not defer:
            task._finish(result)
        return task


@pytest.fixture
def migration_context():
    """提供受控宿主边界，插件本身仍使用实际 SDK 任务和释放对象。"""
    library = ControlledLibrary()
    reports = []
    commands = {}
    events = {}
    ctx = SimpleNamespace(
        plugin_id="official.legacy-migration",
        game=library,
        mods=library,
        tasks=ControlledTasks(),
        storage=MemoryStorage(),
        i18n=TranslationFacade(),
        subscriptions=DisposableStore(),
        commands=SimpleNamespace(register=lambda key, handler: commands.setdefault(key, handler)),
        events=SimpleNamespace(on=lambda key, handler: events.setdefault(key, handler)),
        ui=SimpleNamespace(show_text_report=lambda text, title="": reports.append((text, title)) or "report.txt"),
    )
    ctx.reports = reports
    ctx.registered_commands = commands
    ctx.registered_events = events
    yield ctx
    ctx.subscriptions.dispose()


def make_scan(module, *, count=2, issues=()):
    """生成带两级分类及完整元数据的不可变解析结果。"""
    environment = module.LegacyEnvironment("default", "默认环境", "C:/legacy/home/default", "C:/legacy")
    categories = (
        module.LegacyCategory(("服装",)),
        module.LegacyCategory(("服装", "对象甲"), "character", "C:/legacy/portrait.png"),
    )
    mods = tuple(
        module.LegacyMod(
            sha=f"{index + 1:040x}",
            source_path=f"C:/legacy/resources/{index}.zip",
            name=f"外观 {index + 1}",
            object_name="对象甲",
            category_path=("服装", "对象甲"),
            author="作者甲",
            description="保留说明",
            rating="P",
            tags=("蓝色", "日常"),
            preview_paths=("C:/legacy/preview.png",),
            source_url="https://example.invalid/item",
            size_bytes=1024,
        )
        for index in range(count)
    )
    return module.LegacyScan(environment, categories, mods, tuple(issues), count * 1024)


def ready_controller(load, ctx, scan, qtbot):
    """通过公开扫描流程取得预览，避免直接修改控制器内部状态。"""
    controller = load("controller").MigrationController(ctx)
    controller.discover(scan.environment.root)
    ctx.tasks.finish((scan.environment,))
    controller.scan_environment(scan.environment)
    ctx.tasks.finish(scan)
    assert controller.state == "ready"
    return controller


def test_activation_registers_command_without_querying_game_or_scanning(load_migration, migration_context):
    plugin = load_migration("entry").Plugin()
    plugin.activate(migration_context)
    assert "official.legacy-migration.open" in migration_context.registered_commands
    assert migration_context.game.reads == 0
    assert not migration_context.tasks.calls
    plugin.deactivate()


def test_scan_requires_explicit_copy_confirmation(load_migration, migration_context, qtbot):
    scan = make_scan(load_migration("legacy"))
    controller = ready_controller(load_migration, migration_context, scan, qtbot)
    try:
        assert controller.scan is scan
        assert controller.summary.pending == 2
        assert not migration_context.game.categories
        assert not migration_context.mods.imports
    finally:
        controller.shutdown()


def test_import_orders_categories_and_copies_complete_metadata(load_migration, migration_context, qtbot):
    scan = make_scan(load_migration("legacy"), count=1)
    controller = ready_controller(load_migration, migration_context, scan, qtbot)
    try:
        controller.start_import()
        assert not migration_context.mods.imports
        qtbot.waitUntil(lambda: controller.state == "finished")
        categories = migration_context.game.categories
        assert [(name, values["parent_id"], values["node_type"]) for name, values in categories] == [
            ("服装", "", "category"),
            ("对象甲", "node-1", "character"),
        ]
        assert all(values["exist_ok"] for _, values in categories)
        request = migration_context.mods.imports[0]
        assert request.source_path == "C:/legacy/resources/0.zip"
        assert request.node_id == "node-2"
        assert (request.name, request.author, request.description, request.rating) == (
            "外观 1",
            "作者甲",
            "保留说明",
            "P",
        )
        assert request.tags == ("蓝色", "日常")
        assert request.preview_paths == ("C:/legacy/preview.png",)
        assert request.source_url == "https://example.invalid/item"
        assert request.expected_sha1 == "0000000000000000000000000000000000000001"
        assert controller.summary.succeeded == 1
        assert not migration_context.mods.catalog["mod-1"].enabled
    finally:
        controller.shutdown()


def test_many_synchronous_results_keep_event_loop_responsive(load_migration, migration_context, qtbot):
    scan = make_scan(load_migration("legacy"), count=1200)
    controller = ready_controller(load_migration, migration_context, scan, qtbot)
    heartbeat = QTimer(controller)
    ticks = []
    heartbeat.timeout.connect(lambda: ticks.append(len(migration_context.mods.imports)))
    heartbeat.start(0)
    try:
        controller.start_import()
        qtbot.waitUntil(lambda: controller.state == "finished", timeout=15000)
        assert controller.summary.succeeded == 1200
        assert any(0 < count < 1200 for count in ticks)
    finally:
        heartbeat.stop()
        controller.shutdown()


def test_stop_preserves_committed_item_and_cancels_remaining(load_migration, migration_context, qtbot):
    scan = make_scan(load_migration("legacy"), count=2)
    controller = ready_controller(load_migration, migration_context, scan, qtbot)
    try:
        migration_context.mods.defer_imports = True
        controller.start_import()
        qtbot.waitUntil(lambda: len(migration_context.mods.imports) == 1)
        pending = migration_context.mods.tasks[-1]
        controller.cancel()
        assert pending in migration_context.mods.cancelled
        pending._finish(OperationResult("game-a", ("mod-1",)))
        qtbot.waitUntil(lambda: controller.state == "finished")
        assert controller.summary.succeeded == 1
        assert controller.summary.cancelled == 1
        assert len(migration_context.mods.imports) == 1
    finally:
        controller.shutdown()


def test_retry_only_submits_failed_items(load_migration, migration_context, qtbot):
    scan = make_scan(load_migration("legacy"), count=2)
    migration_context.mods.responses.extend(
        [
            OperationResult("game-a", ("mod-1",)),
            OperationResult("game-a", failed_ids=(scan.mods[1].sha,), error="读取失败"),
        ]
    )
    controller = ready_controller(load_migration, migration_context, scan, qtbot)
    try:
        controller.start_import()
        qtbot.waitUntil(lambda: controller.state == "finished")
        assert (controller.summary.succeeded, controller.summary.failed) == (1, 1)
        controller.retry_import()
        qtbot.waitUntil(lambda: controller.state == "finished")
        assert [request.name for request in migration_context.mods.imports] == ["外观 1", "外观 2", "外观 2"]
        assert (controller.summary.succeeded, controller.summary.failed) == (2, 0)
    finally:
        controller.shutdown()


@pytest.mark.parametrize("reference_game", ["game-a", "game-b", None])
def test_history_skips_only_existing_mod_in_current_game(load_migration, migration_context, qtbot, reference_game):
    scan = make_scan(load_migration("legacy"), count=1)
    migration_context.storage.set("migration_history", {"game-a": {scan.mods[0].sha: "previous"}})
    if reference_game is not None:
        migration_context.mods.catalog["previous"] = ModInfo("previous", "已有外观", game_id=reference_game)
    controller = ready_controller(load_migration, migration_context, scan, qtbot)
    try:
        controller.start_import()
        qtbot.waitUntil(lambda: controller.state == "finished")
        assert controller.summary.skipped == (1 if reference_game == "game-a" else 0)
        assert len(migration_context.mods.imports) == (0 if reference_game == "game-a" else 1)
        expected = "previous" if reference_game == "game-a" else "mod-1"
        assert migration_context.storage.get("migration_history")["game-a"][scan.mods[0].sha] == expected
    finally:
        controller.shutdown()


def test_game_change_discards_late_operation_and_requires_new_scan(load_migration, migration_context, qtbot):
    scan = make_scan(load_migration("legacy"), count=2)
    controller = ready_controller(load_migration, migration_context, scan, qtbot)
    migration_context.mods.immediate = False
    changed = []
    controller.changed.connect(lambda: changed.append(controller.state))
    try:
        controller.start_import()
        qtbot.waitUntil(lambda: bool(migration_context.game.tasks))
        pending = migration_context.game.tasks[-1]
        migration_context.game.game_id = "game-b"
        controller.game_changed("game-b")
        snapshot = list(changed)
        pending._finish(OperationResult("game-a", node_id="old-node"))
        QCoreApplication.processEvents()
        assert changed == snapshot
        assert controller.scan is None
        assert controller.target_game_id == "game-b"
        assert not migration_context.mods.imports
        assert pending in migration_context.mods.cancelled
    finally:
        controller.shutdown()


def test_shutdown_cancels_scan_and_ignores_queued_completion(load_migration, migration_context, qtbot):
    module = load_migration("controller")
    scan = make_scan(load_migration("legacy"))
    controller = module.MigrationController(migration_context)
    controller.discover(scan.environment.root)
    changes = []
    controller.changed.connect(lambda: changes.append(controller.state))
    controller.shutdown()
    snapshot = list(changes)
    migration_context.tasks.finish((scan.environment,))
    QCoreApplication.processEvents()
    assert changes == snapshot
    assert migration_context.tasks.calls[-1].cancelled.is_set()
    assert not any(timer.isActive() for timer in controller.findChildren(QTimer))
    controller.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_host_cancelled_import_stops_the_remaining_queue(load_migration, migration_context, qtbot):
    scan = make_scan(load_migration("legacy"), count=3)
    migration_context.mods.responses.append(OperationResult("game-a", cancelled_ids=(scan.mods[0].sha,)))
    controller = ready_controller(load_migration, migration_context, scan, qtbot)
    try:
        controller.start_import()
        qtbot.waitUntil(lambda: controller.state == "finished")
        assert len(migration_context.mods.imports) == 1
        assert controller.summary.cancelled == 3
    finally:
        controller.shutdown()


def test_cancel_request_without_mod_ids_stops_queue_and_keeps_reason(load_migration, migration_context, qtbot):
    """新 Mod 尚无身份时，宿主取消标志仍须停止队列并保留取消原因。"""
    scan = make_scan(load_migration("legacy"), count=3)
    migration_context.mods.responses.append(OperationResult("game-a", error="导入已停止", cancel_requested=True))
    controller = ready_controller(load_migration, migration_context, scan, qtbot)
    try:
        controller.start_import()
        qtbot.waitUntil(lambda: controller.state == "finished")
        assert len(migration_context.mods.imports) == 1
        assert controller.summary.cancelled == 3
        assert controller.summary.failed == 0
        assert controller.items[0].error == "导入已停止"
        controller.retry_import()
        qtbot.waitUntil(lambda: controller.state == "finished")
        assert controller.summary.succeeded == 3
        assert controller.summary.cancelled == 0
    finally:
        controller.shutdown()


@pytest.mark.parametrize("outcome", ["succeeded", "skipped"])
def test_cancel_request_after_commit_preserves_result_and_stops_queue(
    load_migration, migration_context, qtbot, outcome
):
    """成功或内容重复已提交后收到取消，仍计入原结果，仅取消后续项目。"""
    scan = make_scan(load_migration("legacy"), count=3)
    values = {"succeeded_ids": ("committed-mod",)} if outcome == "succeeded" else {"skipped_ids": ("existing-mod",)}
    migration_context.mods.responses.append(OperationResult("game-a", cancel_requested=True, **values))
    controller = ready_controller(load_migration, migration_context, scan, qtbot)
    try:
        controller.start_import()
        qtbot.waitUntil(lambda: controller.state == "finished")
        assert len(migration_context.mods.imports) == 1
        assert controller.items[0].status == outcome
        assert controller.summary.cancelled == 2
        assert controller.summary.failed == 0
        expected_id = "committed-mod" if outcome == "succeeded" else "existing-mod"
        assert migration_context.storage.get("migration_history")["game-a"][scan.mods[0].sha] == expected_id
    finally:
        controller.shutdown()


def test_committed_mod_keeps_cleanup_error_without_changing_success(load_migration, migration_context, qtbot):
    """清理可能在入库后报错，向导保留错误说明并继续将已提交项目计为成功。"""
    scan = make_scan(load_migration("legacy"), count=2)
    migration_context.mods.responses.append(
        OperationResult("game-a", succeeded_ids=("committed-mod",), error="临时目录清理失败", cancel_requested=True)
    )
    controller = ready_controller(load_migration, migration_context, scan, qtbot)
    try:
        controller.start_import()
        qtbot.waitUntil(lambda: controller.state == "finished")
        assert controller.items[0].error == "临时目录清理失败"
        assert controller.summary.succeeded == 1
        assert controller.summary.failed == 0
        assert controller.summary.cancelled == 1
        assert len(migration_context.mods.imports) == 1
    finally:
        controller.shutdown()


@pytest.mark.parametrize("created", [False, True])
def test_category_cancel_request_stops_queue_without_reporting_failure(
    load_migration, migration_context, qtbot, created
):
    """分类操作没有取消成员 ID 时，也不能继续建立子分类或导入资源。"""
    scan = make_scan(load_migration("legacy"), count=2)
    controller = ready_controller(load_migration, migration_context, scan, qtbot)
    migration_context.game.immediate = False
    try:
        controller.start_import()
        qtbot.waitUntil(lambda: bool(migration_context.game.tasks))
        result = OperationResult(
            "game-a",
            node_id="created-node" if created else "",
            error="" if created else "分类操作已停止",
            cancel_requested=True,
        )
        migration_context.game.tasks[-1]._finish(result)
        QCoreApplication.processEvents()
        assert controller.state == "finished"
        assert len(migration_context.game.categories) == 1
        assert not migration_context.mods.imports
        assert controller.summary.cancelled == 2
        assert controller.summary.failed == 0
        assert not controller.issues
    finally:
        controller.shutdown()


def test_shutdown_during_status_notification_prevents_the_next_host_write(load_migration, migration_context, qtbot):
    """状态信号中的嵌套停用不能被恢复执行的旧调用栈越过。"""
    scan = make_scan(load_migration("legacy"), count=1)
    controller = ready_controller(load_migration, migration_context, scan, qtbot)

    def stop_on_import_status():
        """在请求发送前停止当前会话，模拟嵌套 Qt 生命周期事件。"""
        if controller.summary.running:
            controller.shutdown()

    controller.changed.connect(stop_on_import_status)
    try:
        controller.start_import()
        qtbot.waitUntil(lambda: controller.state == "closed")
        assert not migration_context.mods.imports
    finally:
        controller.shutdown()


def test_game_change_inside_submission_cancels_the_returned_old_task(
    load_migration, migration_context, qtbot, monkeypatch
):
    """宿主调用内部触发游戏切换时，返回的旧任务仍归属旧代次。"""
    scan = make_scan(load_migration("legacy"), count=1)
    controller = ready_controller(load_migration, migration_context, scan, qtbot)
    cancelled = Event()
    pending = OperationTask(cancel=cancelled.set)

    def create_then_change(*args, **kwargs):
        """在任务句柄返回前触发目标切换。"""
        migration_context.game.game_id = "game-b"
        controller.game_changed("game-b")
        return pending

    monkeypatch.setattr(migration_context.game, "create_category", create_then_change)
    try:
        controller.start_import()
        qtbot.waitUntil(lambda: controller.target_game_id == "game-b")
        assert cancelled.is_set()
        pending._finish(OperationResult("game-a", node_id="old-target"))
        assert controller.state == "selecting"
        assert not controller.issues
    finally:
        controller.shutdown()


def test_failed_parent_never_routes_mods_to_another_category(load_migration, migration_context, qtbot):
    scan = make_scan(load_migration("legacy"), count=2)
    controller = ready_controller(load_migration, migration_context, scan, qtbot)
    migration_context.game.immediate = False
    try:
        controller.start_import()
        qtbot.waitUntil(lambda: bool(migration_context.game.tasks))
        migration_context.game.tasks[-1]._finish(OperationResult("game-a", error="分类目录无法写入"))
        qtbot.waitUntil(lambda: controller.state == "finished")
        assert controller.summary.failed == 2
        assert not migration_context.mods.imports
        assert len(migration_context.game.categories) == 1
        assert any(issue.source == "分类目录无法写入" for issue in controller.issues)
    finally:
        controller.shutdown()


def test_mod_without_category_creates_valid_fallback_node(load_migration, migration_context, qtbot):
    scan = make_scan(load_migration("legacy"), count=1)
    source = replace(scan.mods[0], object_name="", category_path=())
    scan = replace(scan, categories=(), mods=(source,))
    controller = ready_controller(load_migration, migration_context, scan, qtbot)
    try:
        assert controller.categories[0].path == ("未分类",)
        controller.start_import()
        qtbot.waitUntil(lambda: controller.state == "finished")
        assert migration_context.game.categories[0][0] == "未分类"
        assert migration_context.mods.imports[0].node_id == "node-1"
        assert controller.summary.succeeded == 1
    finally:
        controller.shutdown()


def test_sdk_duplicate_result_records_actual_target_id(load_migration, migration_context, qtbot):
    scan = make_scan(load_migration("legacy"), count=1)
    migration_context.mods.responses.append(OperationResult("game-a", skipped_ids=("existing-target",)))
    controller = ready_controller(load_migration, migration_context, scan, qtbot)
    try:
        controller.start_import()
        qtbot.waitUntil(lambda: controller.state == "finished")
        assert controller.summary.skipped == 1
        assert controller.summary.succeeded == 0
        assert migration_context.storage.get("migration_history")["game-a"][scan.mods[0].sha] == "existing-target"
    finally:
        controller.shutdown()


def test_completed_batch_exports_duplicate_details_to_text_report(load_migration, migration_context, qtbot):
    scan = make_scan(load_migration("legacy"), count=1)
    migration_context.storage.set("migration_history", {"game-a": {scan.mods[0].sha: "previous"}})
    migration_context.mods.catalog["previous"] = ModInfo("previous", "Existing", game_id="game-a")
    controller = ready_controller(load_migration, migration_context, scan, qtbot)
    try:
        controller.start_import()
        qtbot.waitUntil(lambda: controller.state == "finished")
        assert controller.summary.skipped == 1
        assert len(migration_context.reports) == 1
        text, title = migration_context.reports[0]
        assert scan.mods[0].name in text and scan.mods[0].sha in text
        assert scan.mods[0].source_path in text and "previous" in text
        assert "跳过" in text and "game-a" in text
        assert title
        assert controller.report_path == "report.txt"
    finally:
        controller.shutdown()


def test_selecting_cache_has_separate_history_and_checked_directory_request(load_migration, migration_context, qtbot):
    from dataclasses import replace

    scan = make_scan(load_migration("legacy"), count=1)
    source = replace(
        scan.mods[0],
        archive_path=scan.mods[0].source_path,
        cache_path="C:/legacy/cache",
        cache_digest="a" * 64,
        cache_changed=True,
        cache_size_bytes=2048,
    )
    scan = replace(scan, mods=(source,))
    migration_context.storage.set("migration_history", {"game-a": {source.sha: "original"}})
    migration_context.mods.catalog["original"] = ModInfo("original", "Original", game_id="game-a")
    controller = ready_controller(load_migration, migration_context, scan, qtbot)
    try:
        controller.select_source(0, True)
        assert controller.items[0].source.source_path == source.cache_path
        controller.start_import()
        qtbot.waitUntil(lambda: controller.state == "finished")
        assert controller.summary.succeeded == 1
        request = migration_context.mods.imports[0]
        assert request.source_path == source.cache_path
        assert request.expected_directory_hash == source.cache_digest
        assert not request.expected_sha1
        history = migration_context.storage.get("migration_history")["game-a"]
        assert history[source.sha] == "original"
        assert history[source.sha + ":cache:" + source.cache_digest] == "mod-1"
    finally:
        controller.shutdown()


def test_report_failure_does_not_undo_successful_import(load_migration, migration_context, qtbot):
    def fail_report(*args, **kwargs):
        """模拟报告写入失败，验证已成功导入的内容不会回滚。"""
        raise OSError("report disk error")

    migration_context.ui.show_text_report = fail_report
    controller = ready_controller(
        load_migration, migration_context, make_scan(load_migration("legacy"), count=1), qtbot
    )
    try:
        controller.start_import()
        qtbot.waitUntil(lambda: controller.state == "finished")
        assert controller.summary.succeeded == 1
        assert any("report disk error" in issue.source for issue in controller.issues)
    finally:
        controller.shutdown()


@pytest.mark.parametrize("interrupt", ["close", "game"])
def test_interruption_exports_committed_results_and_unsettled_operation(
    load_migration, migration_context, qtbot, interrupt
):
    scan = make_scan(load_migration("legacy"), count=3)
    migration_context.mods.defer_imports = True
    controller = ready_controller(load_migration, migration_context, scan, qtbot)
    controller.start_import()
    qtbot.waitUntil(lambda: len(migration_context.mods.imports) == 1)
    migration_context.mods.tasks[-1]._finish(OperationResult("game-a", ("mod-1",)))
    qtbot.waitUntil(lambda: len(migration_context.mods.imports) == 2)
    pending = migration_context.mods.tasks[-1]
    if interrupt == "close":
        controller.shutdown()
    else:
        migration_context.game.game_id = "game-b"
        controller.game_changed("game-b")
    assert pending in migration_context.mods.cancelled
    assert len(migration_context.reports) == 1
    text = migration_context.reports[0][0]
    assert "mod-1" in text and "game-a" in text
    assert "最终结果" in text
    assert "正在复制" in text and "已取消" in text
    controller.shutdown()


@pytest.mark.parametrize("cancelled", [False, True])
def test_failed_and_cancelled_results_keep_recovery_warnings_in_report(
    load_migration, migration_context, qtbot, cancelled
):
    warning = "Recovery receipt: C:/recovery/import-cleanup/receipt.json"
    migration_context.mods.responses.append(
        OperationResult("game-a", error="stopped", warnings=(warning,), cancel_requested=cancelled)
    )
    controller = ready_controller(
        load_migration, migration_context, make_scan(load_migration("legacy"), count=1), qtbot
    )
    try:
        controller.start_import()
        qtbot.waitUntil(lambda: controller.state == "finished")
        assert warning in controller.items[0].warnings
        assert warning in migration_context.reports[0][0]
    finally:
        controller.shutdown()


@pytest.mark.parametrize("outcome", ["failed", "cancelled", "succeeded"])
def test_game_switch_report_keeps_known_category_diagnostics(load_migration, migration_context, qtbot, outcome):
    warning = "Category recovery receipt: C:/recovery/category.json"
    controller = ready_controller(
        load_migration, migration_context, make_scan(load_migration("legacy"), count=1), qtbot
    )
    migration_context.game.immediate = False
    try:
        controller.start_import()
        qtbot.waitUntil(lambda: bool(migration_context.game.tasks))
        migration_context.game.game_id = "game-b"
        migration_context.game.tasks[-1]._finish(
            OperationResult(
                "game-a",
                node_id="created-category" if outcome == "succeeded" else "",
                error="Category write failed" if outcome == "failed" else "",
                warnings=(warning,),
                cancel_requested=outcome == "cancelled",
            )
        )
        assert len(migration_context.reports) == 1
        text = migration_context.reports[0][0]
        assert "game-a" in text
        assert warning in text
        if outcome == "failed":
            assert "Category write failed" in text
        assert not migration_context.mods.imports
    finally:
        controller.shutdown()


@pytest.mark.parametrize("outcome", ["failed", "cancelled", "succeeded"])
def test_close_report_keeps_completed_category_diagnostics(
    load_migration, migration_context, qtbot, monkeypatch, outcome
):
    warning = "Completed category receipt: C:/recovery/category.json"
    controller = ready_controller(
        load_migration, migration_context, make_scan(load_migration("legacy"), count=1), qtbot
    )
    pending = OperationTask()
    pending.on_finished(lambda result: controller.shutdown())
    monkeypatch.setattr(migration_context.game, "create_category", lambda *args, **kwargs: pending)
    try:
        controller.start_import()
        qtbot.waitUntil(lambda: controller._task is pending)
        pending._finish(
            OperationResult(
                "game-a",
                node_id="created-category" if outcome == "succeeded" else "",
                error="Category write failed" if outcome == "failed" else "",
                warnings=(warning,),
                cancel_requested=outcome == "cancelled",
            )
        )
        assert controller.state == "closed"
        assert len(migration_context.reports) == 1
        text = migration_context.reports[0][0]
        assert warning in text
        if outcome == "failed":
            assert "Category write failed" in text
        assert not migration_context.mods.imports
    finally:
        controller.shutdown()


def test_scan_can_be_stopped_and_retried_without_importing(load_migration, migration_context, qtbot):
    module = load_migration("controller")
    scan = make_scan(load_migration("legacy"))
    controller = module.MigrationController(migration_context)
    try:
        controller.discover(scan.environment.root)
        migration_context.tasks.finish((scan.environment,))
        controller.scan_environment(scan.environment)
        pending = migration_context.tasks.calls[-1]
        controller.cancel()
        assert pending.cancelled.is_set()
        migration_context.tasks.finish(scan)
        assert controller.state == "cancelled"
        assert controller.scan is None
        controller.scan_environment(scan.environment)
        migration_context.tasks.finish(scan)
        assert controller.state == "ready"
        assert not migration_context.mods.imports
    finally:
        controller.shutdown()


def test_scan_worker_uses_only_thread_safe_task_context(load_migration, migration_context, monkeypatch, qtbot):
    module = load_migration("controller")
    scan = make_scan(load_migration("legacy"))
    main_thread = get_ident()
    calls = []

    def read(environment, *, should_cancel, progress):
        """验证实际提交给工作线程的函数只读取冻结环境和任务上下文。"""
        assert get_ident() != main_thread
        assert environment == scan.environment
        calls.append(should_cancel())
        progress(1, 2, "正在扫描旧版 Mod")
        if should_cancel():
            raise InterruptedError()
        return scan

    monkeypatch.setattr(module, "read_environment", read)
    controller = module.MigrationController(migration_context)
    try:
        controller.discover(scan.environment.root)
        migration_context.tasks.finish((scan.environment,))
        controller.scan_environment(scan.environment)
        submission = migration_context.tasks.calls[-1]
        progress = []
        task_context = TaskContext(submission.cancelled, progress.append)
        with ThreadPoolExecutor(max_workers=1) as executor:
            assert executor.submit(submission.worker, task_context).result(timeout=3) is scan
            submission.cancelled.set()
            with pytest.raises(TaskCancelled):
                executor.submit(submission.worker, task_context).result(timeout=3)
        assert calls == [False]
        assert [(item.fraction, item.message) for item in progress] == [(0.5, "正在扫描旧版 Mod")]
    finally:
        controller.shutdown()
