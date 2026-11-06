"""通过 SDK 串行复制旧版资源，所有宿主读写及结果处理均留在界面线程。"""

from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass, replace
from functools import partial

from PySide6.QtCore import QObject, QTimer, Signal

from src.plugin_sdk import (
    DisposableStore,
    ModImportRequest,
    OperationResult,
    PluginContext,
    TaskCancelled,
    TaskContext,
    TaskProgress,
    TaskResult,
)

from .legacy import LegacyCategory, LegacyEnvironment, LegacyMod, LegacyScan, ScanIssue, discover_environments
from .legacy import scan_environment as read_environment
from .report import migration_report


@dataclass(frozen=True)
class MigrationItem:
    """保留单个源条目的结果，重试时仅重置失败及取消条目。"""

    source: LegacyMod
    status: str = "pending"
    target_id: str = ""
    error: str = ""
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class MigrationSummary:
    """按源条目统计结果，避免把旧版 SHA 当成目标 Mod 身份。"""

    total: int = 0
    pending: int = 0
    running: int = 0
    succeeded: int = 0
    skipped: int = 0
    failed: int = 0
    cancelled: int = 0


def _discover(task: TaskContext, root: str) -> tuple[LegacyEnvironment, ...]:
    """只把用户选择的路径交给后台，不在工作线程访问插件上下文。"""
    task.raise_if_cancelled()
    environments = discover_environments(root)
    task.raise_if_cancelled()
    return environments


def _scan(task: TaskContext, environment: LegacyEnvironment) -> LegacyScan:
    """把解析器的取消和进度协议转换为宿主后台任务协议。"""

    def cancelled() -> bool:
        """读取线程安全的取消信号。"""
        return task.cancelled

    def progress(completed: int, total: int, message: str) -> None:
        """只投递数值和文本，不触碰任何 Qt 控件。"""
        task.report_progress(min(1.0, max(0.0, completed / max(total, 1))), message)

    task.raise_if_cancelled()
    try:
        return read_environment(environment, should_cancel=cancelled, progress=progress)
    except InterruptedError:
        raise TaskCancelled() from None


class MigrationController(QObject):
    """用有所有者的单次定时器推进业务任务，防止同步结果造成递归调用。"""

    changed = Signal()

    def __init__(self, ctx: PluginContext, parent: QObject | None = None) -> None:
        """在向导打开时才读取目标游戏，并初始化可取消的会话状态。"""
        super().__init__(parent)
        self.ctx = ctx
        self.state = "idle"
        self.message = ctx.i18n.t("选择旧版安装目录，读取可迁移的用户环境。")
        self.root = ""
        self.target_game_id = ctx.game.current_game_id()
        self.target_game_name = ctx.game.current_game_name()
        self.environments: tuple[LegacyEnvironment, ...] = ()
        self.scan: LegacyScan | None = None
        self.categories: tuple[LegacyCategory, ...] = ()
        self.items: list[MigrationItem] = []
        self.migration_issues: list[ScanIssue] = []
        self.progress = 0.0
        self.progress_message = ""
        self.report_path = ""
        self._counts: Counter[str] = Counter()
        self._alive = True
        self._generation = 0
        self._task = None
        self._active_operation: tuple[str, LegacyCategory | int] | None = None
        self._background_handles = DisposableStore()
        self._stop_requested = False
        self._steps = deque()
        self._nodes: dict[tuple[str, ...], str] = {(): ""}
        self._category_errors: dict[tuple[str, ...], str] = {}
        self._history: dict[str, dict[str, str]] = {}
        self._history_dirty = 0
        self._history_warning = False
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._advance)

    @property
    def busy(self) -> bool:
        """扫描、复制和等待安全停止期间禁止更换来源。"""
        return self.state in {"discovering", "scanning", "importing", "stopping"}

    @property
    def summary(self) -> MigrationSummary:
        """使用增量计数生成快照，大库逐项完成时不反复遍历全部条目。"""
        return MigrationSummary(total=len(self.items), **self._counts)

    @property
    def issues(self) -> tuple[ScanIssue, ...]:
        """同时展示源扫描问题和本次复制产生的诊断。"""
        return (self.scan.issues if self.scan is not None else ()) + tuple(self.migration_issues)

    def _emit(self) -> None:
        """仅向仍有效的窗口发布状态。"""
        if self._alive:
            self.changed.emit()

    def _valid(self, generation: int) -> bool:
        """旧任务完成后不得访问已关闭窗口或新的扫描会话。"""
        return self._alive and generation == self._generation

    def _invalidate(self) -> None:
        """先使回调失效，再请求取消，兼容取消入口同步交付结果。"""
        self._generation += 1
        self._timer.stop()
        self._background_handles.dispose()
        task, self._task = self._task, None
        self._active_operation = None
        if task is not None:
            task.cancel()
        self._steps.clear()

    def _clear_preview(self) -> None:
        """清除只属于旧扫描目标的预览及计数。"""
        self.scan = None
        self.categories = ()
        self.items = []
        self._counts.clear()
        self.migration_issues = []
        self.report_path = ""
        self.progress = 0.0
        self.progress_message = ""

    def discover(self, root: str) -> None:
        """在用户明确读取目录后提交后台环境发现。"""
        if not self._alive or self.busy:
            return
        root = root.strip()
        if not root:
            self.message = self.ctx.i18n.t("请先选择旧版安装目录。")
            self._emit()
            return
        self._invalidate()
        self._clear_preview()
        self.root = root
        self.environments = ()
        self._stop_requested = False
        self.state = "discovering"
        self.message = self.ctx.i18n.t("正在读取旧版环境…")
        self._emit()
        self._background(partial(_discover, root=root), self.message, self._discovered)

    def _background(self, worker, title: str, callback) -> None:
        """将后台观察者限于当前任务，完成后及时释放进度订阅。"""
        generation = self._generation
        self._background_handles = DisposableStore()
        try:
            self._task = self.ctx.tasks.run(worker, title=title)
            self._background_handles.add(self._task.on_progress(partial(self._progressed, generation)))
            self._background_handles.add(self._task.on_finished(partial(callback, generation)))
        except Exception as error:
            self._task = None
            self._background_handles.dispose()
            self.state = "error"
            self.message = self.ctx.i18n.t("无法启动任务：{error}", error=str(error))
            self._emit()

    def _progressed(self, generation: int, progress: TaskProgress) -> None:
        """在宿主线程展示进度，停止状态不再被旧进度覆盖。"""
        if self._valid(generation) and not self._stop_requested:
            self.progress = progress.fraction
            self.progress_message = self.ctx.i18n.t(progress.message)
            self._emit()

    def _finish_background(self, generation: int, result: TaskResult) -> bool:
        """统一收回后台任务，并将取消与异常保持为可重试状态。"""
        if not self._valid(generation):
            return False
        self._task = None
        self._background_handles.dispose()
        self.progress_message = ""
        if self._stop_requested or result.cancelled:
            self.state = "cancelled"
            self.message = self.ctx.i18n.t("扫描已停止，可以重新扫描。")
        elif not result.ok:
            self.state = "error"
            self.message = self.ctx.i18n.t("读取旧版数据失败：{error}", error=self.ctx.i18n.t(result.error))
        else:
            return True
        self._emit()
        return False

    def _discovered(self, generation: int, result: TaskResult) -> None:
        """读取环境列表后等待用户选择，不自动执行扫描或写入。"""
        if not self._finish_background(generation, result):
            return
        self.environments = tuple(result.value or ())
        self.state = "selecting"
        self.message = (
            self.ctx.i18n.t("选择一个旧版环境，然后扫描预览。")
            if self.environments
            else self.ctx.i18n.t("未找到旧版环境，请选择包含 home 文件夹的旧版安装目录。")
        )
        self._emit()

    def scan_environment(self, environment: LegacyEnvironment) -> None:
        """冻结本次预览的游戏身份后扫描选定环境。"""
        if not self._alive or self.busy or environment not in self.environments:
            return
        game_id = self.ctx.game.current_game_id()
        if not game_id:
            self.message = self.ctx.i18n.t("请先在游戏页创建并选择目标游戏。")
            self._emit()
            return
        self._invalidate()
        self._clear_preview()
        self.target_game_id = game_id
        self.target_game_name = self.ctx.game.current_game_name()
        self._stop_requested = False
        self.state = "scanning"
        self.message = self.ctx.i18n.t("正在扫描本地资源…")
        self._emit()
        self._background(partial(_scan, environment=environment), self.message, self._scanned)

    def _scanned(self, generation: int, result: TaskResult) -> None:
        """完整扫描结果仅进入预览阶段，复制仍需单独确认。"""
        if not self._finish_background(generation, result) or not self._check_game():
            return
        if not isinstance(result.value, LegacyScan):
            self.state = "error"
            self.message = self.ctx.i18n.t("扫描未返回有效结果，请重新扫描。")
        else:
            self.scan = result.value
            self.items = [MigrationItem(mod) for mod in self.scan.mods]
            self.categories = self._collect_categories()
            self._counts = Counter(item.status for item in self.items)
            self.state = "ready"
            self.message = self.ctx.i18n.t("请检查预览和扫描问题，确认后开始复制。")
            self.progress = 1.0
        self._emit()

    def _check_game(self) -> bool:
        """每次提交及接收结果时再次验证游戏，兼容尚未投递的切换事件。"""
        current = self.ctx.game.current_game_id()
        if current and current == self.target_game_id:
            return True
        self.game_changed(current)
        return False

    def game_changed(self, game_id: str) -> None:
        """切换目标使扫描和旧任务失效，已提交到旧游戏的成功项目仍由宿主保留。"""
        if not self._alive or game_id == self.target_game_id:
            return
        self._interruption_report()
        self._save_history()
        self._invalidate()
        self._clear_preview()
        self.target_game_id = game_id
        self.target_game_name = self.ctx.game.current_game_name()
        self.state = "selecting" if self.environments else "idle"
        self.message = self.ctx.i18n.t("目标游戏已更改，旧任务已停止。请重新扫描后再复制。")
        self._emit()

    def destination_path(self, mod: LegacyMod) -> tuple[str, ...]:
        """无归属条目进入未分类节点，始终为 SDK 提供有效的目标分类。"""
        if mod.category_path:
            return mod.category_path
        root = (self.ctx.i18n.t("未分类"),)
        return root + (mod.object_name,) if mod.object_name else root

    def select_source(self, index: int, use_cache: bool) -> None:
        """只在确认前更换选定条目的来源，原包默认优先，缓存身份独立记录。"""
        if not self._alive or self.state != "ready" or not 0 <= index < len(self.items):
            return
        item = self.items[index]
        source = item.source
        path = source.cache_path if use_cache else source.archive_path
        if not path or item.status != "pending":
            return
        size = (
            source.cache_size_bytes
            if use_cache
            else next(mod.size_bytes for mod in self.scan.mods if mod.sha == source.sha)
        )
        self.items[index] = replace(item, source=replace(source, source_path=path, size_bytes=size))
        self._emit()

    @staticmethod
    def _history_key(source: LegacyMod) -> str:
        """缓存的内容指纹与原包 SHA 分离，避免原包记录吞掉用户修改。"""
        if source.cache_path and source.source_path == source.cache_path:
            return (source.sha or source.legacy_key) + ":cache:" + source.cache_digest
        return source.sha

    def _collect_categories(self) -> tuple[LegacyCategory, ...]:
        """补齐缺失祖先，让预览与实际复制使用相同分类路径。"""
        categories = {category.path: category for category in self.scan.categories if category.path}
        for item in self.items:
            path = self.destination_path(item.source)
            for depth in range(1, len(path) + 1):
                prefix = path[:depth]
                node_type = "character" if depth == len(path) and item.source.object_name else "category"
                categories.setdefault(prefix, LegacyCategory(prefix, node_type))
        for path in tuple(categories):
            for depth in range(1, len(path)):
                prefix = path[:depth]
                categories.setdefault(prefix, LegacyCategory(prefix))
        return tuple(sorted(categories.values(), key=lambda value: len(value.path)))

    def _build_steps(self) -> None:
        """分类按父级优先排列，失败分类不会回退到错误目标。"""
        self._steps = deque(("category", value) for value in self.categories)
        self._steps.extend(("mod", index) for index, item in enumerate(self.items) if item.status == "pending")
        self._nodes = {(): ""}
        self._category_errors = {}

    def start_import(self, *, retry: bool = False) -> None:
        """从已确认预览开始复制，重试保留成功和跳过条目的结果。"""
        if not self._alive or self.busy or self.scan is None or self.state not in {"ready", "finished"}:
            return
        if not self._check_game():
            return
        if retry:
            for index, item in enumerate(self.items):
                if item.status in {"failed", "cancelled"}:
                    self._set_item(index, "pending")
        elif self.state == "finished":
            return
        self._generation += 1
        self._stop_requested = False
        self.migration_issues = []
        self._history_warning = False
        self._read_history()
        self._build_steps()
        self.state = "importing"
        self.message = self.ctx.i18n.t("正在复制到当前游戏…")
        self.progress = 0.0
        self._emit()
        self._schedule()

    def retry_import(self) -> None:
        """只重新提交上次失败或取消的项目。"""
        if self.summary.failed or self.summary.cancelled:
            self.start_import(retry=True)

    def _schedule(self) -> None:
        """每轮仅推进一项，始终把控制权交回 Qt 事件循环。"""
        if self._alive and self.state == "importing" and self._task is None:
            self._timer.start(0)

    def _advance(self) -> None:
        """在宿主线程提交下一个分类或 Mod，请求完成后才继续。"""
        if not self._alive or self.state != "importing" or not self._check_game():
            return
        if self._stop_requested or not self._steps:
            self._finish_import()
            return
        kind, value = self._steps.popleft()
        if kind == "category":
            self._create_category(value)
        else:
            self._import_item(value)

    def _create_category(self, category: LegacyCategory) -> None:
        """复用同级同名同类型节点，旧缩略图只用于创建新节点。"""
        generation = self._generation
        parent = category.path[:-1]
        if parent not in self._nodes:
            self._category_errors[category.path] = self._category_errors.get(
                parent, self.ctx.i18n.t("上级分类创建失败。")
            )
            self._schedule()
            return
        try:
            task = self.ctx.game.create_category(
                category.path[-1],
                parent_id=self._nodes[parent],
                node_type=category.node_type,
                thumbnail_path=category.thumbnail_path,
                exist_ok=True,
            )
            self._observe(task, "category", category, generation)
        except Exception as error:
            if not self._valid(generation):
                return
            self._category_failed(category, str(error))
            self._emit()
            self._schedule()

    def _category_failed(self, category: LegacyCategory, error: str) -> None:
        """保留分类失败原因，禁止把它的资源导入其他父节点。"""
        self._category_errors[category.path] = error
        self.migration_issues.append(
            ScanIssue("invalid", self.ctx.i18n.t("目标分类创建失败。"), " / ".join(category.path), error)
        )

    def _record_category_result(self, category: LegacyCategory, result: OperationResult) -> None:
        """先保留已知分类结果和诊断，再允许游戏切换或窗口关闭使会话失效。"""
        if result.ok and result.node_id:
            self._nodes[category.path] = result.node_id
        elif not (result.cancel_requested or result.cancelled_ids):
            self._category_failed(category, result.error or self.ctx.i18n.t("目标分类创建失败。"))
        for warning in result.warnings:
            self.migration_issues.append(ScanIssue("warning", warning, " / ".join(category.path)))

    def _import_item(self, index: int) -> None:
        """先核实迁移记录仍指向当前游戏，再交给宿主校验摘要及复制文件。"""
        generation = self._generation
        item = self.items[index]
        source = item.source
        path = self.destination_path(source)
        node_id = self._nodes.get(path)
        if not node_id:
            self._set_item(index, "failed", error=self._category_errors.get(path, self.ctx.i18n.t("目标分类不可用。")))
            self._emit()
            self._schedule()
            return
        try:
            previous = self._history.get(self.target_game_id, {}).get(self._history_key(source))
            existing = self.ctx.mods.get_mod(previous) if previous else None
            if not self._valid(generation) or self.state != "importing":
                return
            if existing is not None and existing.game_id == self.target_game_id:
                self._set_item(index, "skipped", target_id=existing.mod_id)
                self._emit()
                self._schedule()
                return
            self._set_item(index, "running")
            self.progress_message = self.ctx.i18n.t("正在复制：{name}", name=source.name)
            self._emit()
            if not self._valid(generation) or self.state != "importing" or not self._check_game():
                return
            request = ModImportRequest(
                source_path=source.source_path,
                node_id=node_id,
                name=source.name,
                author=source.author,
                description=source.description,
                rating=source.rating,
                tags=source.tags,
                preview_paths=source.preview_paths,
                source_url=source.source_url or None,
                expected_sha1="" if source.cache_path and source.source_path == source.cache_path else source.sha,
                expected_directory_hash=source.cache_digest
                if source.cache_path and source.source_path == source.cache_path
                else "",
            )
            self._observe(self.ctx.mods.import_mod(request), "mod", index, generation)
        except Exception as error:
            if not self._valid(generation):
                return
            self._set_item(index, "failed", error=str(error))
            self._emit()
            self._schedule()

    def _observe(self, task, kind: str, value, generation: int) -> None:
        """先登记任务再订阅，兼容 SDK 对已完成任务立即调用观察者。"""
        if not self._valid(generation):
            task.cancel()
            return
        self._task = task
        self._active_operation = (kind, value)
        task.on_finished(partial(self._operated, generation, kind, value))
        if self._valid(generation) and self._stop_requested and not task.done:
            self.state = "stopping"
            self._emit()
            task.cancel()

    def _operated(self, generation: int, kind: str, value, result: OperationResult) -> None:
        """把已提交结果计入统计，宿主取消标志和取消成员均终止后续工作。"""
        if not self._valid(generation):
            return
        self._task = None
        self._active_operation = None
        if result.game_id == self.target_game_id:
            if kind == "category":
                self._record_category_result(value, result)
            else:
                self._record_result(value, result)
        if not self._check_game():
            return
        cancel_requested = result.cancel_requested or bool(result.cancelled_ids)
        if cancel_requested:
            self._stop_requested = True
        if result.game_id != self.target_game_id:
            self._stop_requested = True
            error = self.ctx.i18n.t("操作结果不属于当前目标游戏，复制已停止。")
            if kind == "category":
                self._category_failed(value, error)
            else:
                self._set_item(value, "failed", error=error)
        self._emit()
        if not self._valid(generation):
            return
        if self._stop_requested:
            self._finish_import()
        else:
            self._schedule()

    def _record_result(self, index: int, result: OperationResult) -> None:
        """先保留成功与重复内容的目标身份，再区分尚无 Mod 身份的取消与失败。"""
        if result.succeeded_ids:
            self._set_item(
                index, "succeeded", target_id=result.succeeded_ids[0], error=result.error, warnings=result.warnings
            )
        elif result.skipped_ids:
            self._set_item(
                index, "skipped", target_id=result.skipped_ids[0], error=result.error, warnings=result.warnings
            )
        elif result.cancel_requested or result.cancelled_ids:
            self._set_item(index, "cancelled", error=result.error, warnings=result.warnings)
        else:
            self._set_item(
                index, "failed", error=result.error or self.ctx.i18n.t("宿主未返回导入结果。"), warnings=result.warnings
            )
        item = self.items[index]
        if item.target_id:
            self._history.setdefault(self.target_game_id, {})[self._history_key(item.source)] = item.target_id
            self._history_dirty += 1
            if self._history_dirty >= 25:
                self._save_history()

    def _set_item(self, index: int, status: str, **values) -> None:
        """更新一个不可变结果并维护常数时间的统计。"""
        previous = self.items[index]
        self._counts[previous.status] -= 1
        self._counts[status] += 1
        fields = {"target_id": "", "error": "", "warnings": (), **values}
        self.items[index] = replace(previous, status=status, **fields)
        settled = sum(self._counts[key] for key in ("succeeded", "skipped", "failed", "cancelled"))
        self.progress = settled / max(1, len(self.items))

    def _read_history(self) -> None:
        """历史仅用于加速重复识别，损坏或读取失败不能替代宿主内容校验。"""
        self._history = {}
        self._history_dirty = 0
        try:
            history = self.ctx.storage.get("migration_history", {})
            if isinstance(history, dict):
                self._history = {
                    game_id: {
                        sha: mod_id
                        for sha, mod_id in entries.items()
                        if isinstance(sha, str) and isinstance(mod_id, str)
                    }
                    for game_id, entries in history.items()
                    if isinstance(game_id, str) and isinstance(entries, dict)
                }
        except Exception as error:
            self._history_issue(str(error))

    def _history_issue(self, detail: str) -> None:
        """存储故障只影响重复识别加速，保留一次可见警告。"""
        if not self._history_warning:
            self._history_warning = True
            self.migration_issues.append(
                ScanIssue("warning", self.ctx.i18n.t("迁移记录无法保存，重试时将重新核对文件内容。"), source=detail)
            )

    def _save_history(self) -> None:
        """分批保存来源摘要映射；中断后仍可由宿主内容去重补全记录。"""
        if not self._history_dirty:
            return
        try:
            self.ctx.storage.set("migration_history", self._history)
            self._history_dirty = 0
        except Exception as error:
            self._history_issue(str(error))

    def cancel(self) -> None:
        """停止后续调度，等待当前操作的安全边界结果后统计。"""
        if not self._alive or not self.busy or self._stop_requested:
            return
        importing = self.state == "importing"
        self._stop_requested = True
        self._timer.stop()
        self.state = "stopping"
        self.progress_message = ""
        self.message = self.ctx.i18n.t("正在停止，请等待当前项目完成安全处理…")
        self._emit()
        if self._task is not None:
            self._task.cancel()
        elif importing:
            self._finish_import()

    def _finish_import(self) -> None:
        """保留成功项，把尚未开始的条目标为取消，并提供可重试结果。"""
        if self._stop_requested:
            for index, item in enumerate(self.items):
                if item.status in {"pending", "running"}:
                    self._set_item(index, "cancelled")
        self._steps.clear()
        self._save_history()
        self.state = "finished"
        self.progress = 1.0
        self.progress_message = ""
        self.message = (
            self.ctx.i18n.t("复制已停止，已完成的项目会保留。")
            if self._stop_requested
            else self.ctx.i18n.t("本次复制已结束，可在当前游戏中查看结果。")
        )
        self.open_report()
        self._emit()

    def open_report(self) -> None:
        """完成后由宿主生成并打开临时 TXT；报告错误不撤销已提交的 Mod。"""
        if not self._alive or self.state != "finished" or self.scan is None:
            return
        self._write_report()

    def _write_report(self) -> None:
        """将当前冻结结果交给宿主，报告生成失败只追加诊断。"""
        generation = self._generation
        try:
            path = self.ctx.ui.show_text_report(migration_report(self), self.ctx.i18n.t("旧版迁移报告"))
            if self._valid(generation):
                self.report_path = path
        except Exception as error:
            if self._valid(generation):
                self.migration_issues.append(
                    ScanIssue(
                        "warning", self.ctx.i18n.t("迁移报告无法生成或打开，请查看窗口中的结果。"), source=str(error)
                    )
                )

    def _interruption_report(self) -> None:
        """关闭或换游戏时保存已知结果，未结算操作不能谎报为成功取消。"""
        if self.state not in {"importing", "stopping"} or self.scan is None:
            return
        task, operation = self._task, self._active_operation
        self._invalidate()
        if operation is not None and task is not None and task.done:
            result = task.result
            if isinstance(result, OperationResult) and result.game_id == self.target_game_id:
                kind, value = operation
                if kind == "category":
                    self._record_category_result(value, result)
                else:
                    self._record_result(value, result)
        for item_index, item in enumerate(self.items):
            if item.status == "pending":
                self._set_item(item_index, "cancelled")
        if (operation is not None and task is not None and not task.done) or any(
            item.status == "running" for item in self.items
        ):
            self.migration_issues.append(
                ScanIssue("warning", "迁移已中断，处理中项目的最终结果请以宿主资源库和任务中心为准。")
            )
        self.state = "finished"
        self._write_report()

    def shutdown(self) -> None:
        """关闭或停用后先禁用回调，随后取消任务并释放定时器。"""
        if not self._alive:
            return
        self._interruption_report()
        self._save_history()
        self._alive = False
        self._invalidate()
        self.state = "closed"
