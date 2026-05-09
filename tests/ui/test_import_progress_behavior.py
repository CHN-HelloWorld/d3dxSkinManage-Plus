from __future__ import annotations

from pathlib import Path

import pytest

from src.core.mod_intake import IntakeRequest, ModIntakeService, PreparedIntake
from src.core.mod_manager import ModManager
from src.core.mod_package import write_container_marker
from src.models.mod import Mod, ModStatus
from src.ui.dialogs.import_progress import (
    ImportProgressDialog,
    ImportWorkerThread,
    ParallelImportWorkerThread,
)
from src.ui.widgets.task_center import TaskCenterPanel
from src.utils import fs


@pytest.fixture(autouse=True)
def registered_game(temp_data_dir):
    # 类别外键约束要求先注册游戏。
    from tests.helpers import register_games

    register_games("game_a")


class _FakeWorker:
    def __init__(self) -> None:
        self.paused = False
        self.acknowledged = 0
        self.created_paths: list[Path] = []

    def pause(self) -> None:
        self.paused = True

    def resume(self) -> None:
        self.paused = False

    def acknowledge_db_done(self) -> None:
        self.acknowledged += 1

    def isRunning(self) -> bool:
        return False

    def get_created_paths(self) -> list[Path]:
        return self.created_paths

    def get_created_preview_caches(self) -> list[Path]:
        return []


def _dialog(qtbot, temp_data_dir) -> ImportProgressDialog:
    mm = ModManager()
    mm.set_game("game_a")
    char = mm.add_character("角色", node_type="character")
    panel = TaskCenterPanel(None, embedded=True)
    dlg = ImportProgressDialog([("", "A")] * 4, char.id, mm, panel=panel)
    panel.setParent(dlg)
    qtbot.addWidget(dlg)
    return dlg


def test_coarse_progress_never_rolls_back_stage_progress(qtbot, temp_data_dir):
    dlg = _dialog(qtbot, temp_data_dir)
    dlg._on_stage_progress(0.63, "复制中")
    dlg._on_progress(1, 4, "第一个 Mod 完成")

    # 进度渲染通道已从 QProgressBar(0~1000) 换成任务中心环(0~100)，数值不变量不变：
    # 粗进度 250 不得把细进度 630 拽回去。
    assert dlg._view._ring._progress == 63.0
    assert dlg._display_progress_value == 630


def test_pause_freezes_progress_until_resume(qtbot, temp_data_dir):
    dlg = _dialog(qtbot, temp_data_dir)
    worker = _FakeWorker()
    dlg._worker = worker
    dlg._on_stage_progress(0.42, "处理中")

    dlg._on_pause()
    dlg._on_stage_progress(0.81, "后台不应推动界面")
    dlg._on_progress(3, 4, "粗进度也不应推动界面")

    assert worker.paused
    assert dlg._view._ring._progress == 42.0
    assert dlg._display_progress_value == 420

    dlg._on_resume()
    assert not worker.paused
    assert dlg._view._ring._progress == 81.0
    assert dlg._display_progress_value == 810


def test_pause_defers_prepared_result_registration(qtbot, temp_data_dir, monkeypatch):
    dlg = _dialog(qtbot, temp_data_dir)
    worker = _FakeWorker()
    dlg._worker = worker
    registered: list[dict] = []
    monkeypatch.setattr(
        dlg,
        "_register_mod_in_db",
        lambda result: registered.append(result) or object(),
    )

    dlg._on_pause()
    result = {"name": "queued"}
    dlg._on_file_prepared(2, result)

    assert registered == []
    assert dlg._completed_results == []
    assert worker.acknowledged == 0

    dlg._on_resume()

    assert registered == [result]
    assert len(dlg._completed_results) == 1
    assert worker.acknowledged == 1


def test_prepared_signal_arriving_after_terminate_is_not_registered(
    qtbot,
    temp_data_dir,
    monkeypatch,
):
    dlg = _dialog(qtbot, temp_data_dir)
    worker = _FakeWorker()
    dlg._worker = worker
    registered: list[dict] = []
    monkeypatch.setattr(
        dlg,
        "_register_mod_in_db",
        lambda result: registered.append(result) or object(),
    )

    dlg._was_terminated = True
    dlg._on_file_prepared(1, {"name": "late"})

    assert registered == []
    assert dlg._completed_results == []
    assert worker.acknowledged == 1


def test_dialog_commits_prepared_intake_through_unified_service(
    qtbot,
    temp_data_dir,
):
    dlg = _dialog(qtbot, temp_data_dir)
    prepared = object.__new__(PreparedIntake)
    committed = object()
    calls: list[PreparedIntake] = []

    class _Service:
        def commit(self, value):
            calls.append(value)
            return committed

    dlg._intake_service = _Service()

    assert dlg._register_mod_in_db(prepared) is committed
    assert calls == [prepared]


def test_commit_unexpected_error_still_releases_import_worker(qtbot, temp_data_dir):
    dlg = _dialog(qtbot, temp_data_dir)
    worker = _FakeWorker()
    dlg._worker = worker
    prepared = object.__new__(PreparedIntake)

    class _Service:
        def commit(self, _value):
            raise RuntimeError("database connection dropped")

    dlg._intake_service = _Service()

    dlg._on_file_prepared(0, prepared)

    assert dlg._completed_results == [(0, None)]
    assert worker.acknowledged == 1


def test_worker_prepares_planned_request_through_unified_service(
    temp_data_dir,
    tmp_path,
):
    source = tmp_path / "Skin"
    source.mkdir()
    request = IntakeRequest(
        source_path=source,
        game_id="game_a",
        node_id="character",
    )
    prepared = object()
    calls = []

    class _Service:
        def prepare(self, value, **kwargs):
            calls.append((value, kwargs))
            return prepared

    worker = ImportWorkerThread(
        items=[],
        intake_service=_Service(),
    )

    assert worker._prepare_request(request, item_index=0, total_items=1) is prepared
    assert calls[0][0] is request
    assert callable(calls[0][1]["progress"])
    assert callable(calls[0][1]["should_cancel"])


def test_parallel_worker_reports_planned_request_without_tuple_access(tmp_path):
    request = IntakeRequest(
        source_path=tmp_path / "Skin",
        game_id="game_a",
        node_id="character",
        display_name="Display Skin",
    )
    prepared = object()
    emitted = []
    progress = []

    class _Service:
        def prepare(self, value, **_kwargs):
            assert value is request
            return prepared

    worker = ParallelImportWorkerThread(
        items=[request],
        intake_service=_Service(),
        max_workers=1,
    )

    def accept(index, result) -> None:
        emitted.append((index, result))
        worker.acknowledge_db_done()

    worker.file_prepared.connect(accept)
    worker.progress_updated.connect(
        lambda current, total, text: progress.append((current, total, text)),
    )
    worker.run()

    assert emitted == [(0, prepared)]
    assert any("Display Skin" in text for _current, _total, text in progress)


def test_import_workers_reject_legacy_tuple_items(tmp_path):
    with pytest.raises(TypeError, match="IntakeRequest"):
        ImportWorkerThread(
            items=[("source", "name")],
            intake_service=object(),
        )


def test_dialog_plans_all_worker_requests_on_main_thread(
    qtbot,
    temp_data_dir,
    tmp_path,
):
    dlg = _dialog(qtbot, temp_data_dir)
    source = tmp_path / "Skin"
    source.mkdir()
    dlg._items = [(str(source), "Display", "preview.png", "R")]

    requests = dlg._plan_intake_requests()

    assert len(requests) == 1
    request = requests[0]
    assert request.source_path == source
    assert request.display_name == "Display"
    assert request.selected_preview_name == "preview.png"
    assert request.age_rating == "R"
    assert request.destination_path == Path(dlg._dest_parent)


def test_long_path_cancel_cleanup_removes_metadata_only_ghost(
    qtbot,
    temp_data_dir,
    tmp_path,
):
    dlg = _dialog(qtbot, temp_data_dir)
    worker = _FakeWorker()
    dlg._worker = worker

    dest = tmp_path / "library"
    for i in range(8):
        dest /= f"segment_{i}_" + "x" * 25
    ghost = dest / "示例角色戌"
    override = ghost / ".d3dxmeta" / "ini_overrides" / "regional_sampleunit_outfits"
    fs.write_text(override / "nested" / "SampleUnit.ini", "[KeySwap]\nkey = p\n")
    kept_container = dest / "示例角色壬"
    kept_body = kept_container / "示例角色壬.7z"
    fs.write_bytes(kept_body, b"kept")
    write_container_marker(kept_container, kept_body.name, "0" * 40, imported_at="2026-10-07T12:00:00")
    kept_mod = Mod(
        mod_id="1" * 32,
        name="示例角色壬",
        folder_name="示例角色壬",
        path=str(kept_body),
        status=ModStatus.DISABLED,
    )
    assert len(str(override)) > 260
    worker.created_paths = [ghost, kept_container]
    dlg._dest_parent = str(dest)
    dlg._pre_existing_names = set()
    dlg._completed_results = [(0, kept_mod)]

    dlg._cleanup_orphaned_files(dlg._completed_keep_names())

    assert not fs.exists(ghost), "终止导入后不应残留只含 .d3dxmeta 的幽灵容器"
    assert fs.exists(kept_body), "选择保留已导入 Mod 时不得误删已提交容器"


def test_preview_and_ini_extraction_happen_before_compression(
    temp_data_dir,
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "示例角色壬"
    fs.write_text(
        source / "regional_iota_outfits" / "MasterIota.ini",
        "[KeySwap]\nkey = p\n",
    )
    fs.write_bytes(source / "images" / "preview.png", b"\x89PNG\r\n\x1a\n")

    mm = ModManager()
    mm.set_game("game_a")
    char = mm.add_character("角色", node_type="character")
    service = ModIntakeService(mm)
    request = service.plan_request(
        source_path=source,
        game_id=mm.current_game_id,
        node_id=char.id,
        target_format="zip",
        selected_preview_name="images/preview.png",
    )

    compressed = False
    original_compress = mm._archive.compress
    original_list = mm._archive.list_contents
    original_read_inis = mm._archive.read_all_inis

    def _compress(*args, **kwargs):
        nonlocal compressed
        result = original_compress(*args, **kwargs)
        compressed = True
        return result

    def _list_contents(path):
        if compressed:
            raise AssertionError("压缩完成后不应为提取数据重新打开成品包")
        return original_list(path)

    def _read_all_inis(path):
        if compressed:
            raise AssertionError("压缩完成后不应重新打开成品包播种 ini")
        return original_read_inis(path)

    monkeypatch.setattr(mm._archive, "compress", _compress)
    monkeypatch.setattr(mm._archive, "list_contents", _list_contents)
    monkeypatch.setattr(mm._archive, "read_all_inis", _read_all_inis)

    result = service.prepare(request)

    assert result is not None
    body = result.body_path
    override = body.parent / ".d3dxmeta" / "ini_overrides" / "regional_iota_outfits" / "MasterIota.ini"
    assert fs.is_file(override)
    assert result.preview_paths and all(fs.is_file(path) for path in result.preview_paths)
    service.rollback(result)


class _RecordingView:
    """只记录终态调用的面板替身。

    _NoopView 会把 finish 静默忽略，测不出顶层终态；真 TaskCenterPanel 需要 QWidget
    宿主与定时器，对「顶层是否报失败」这条不变量是多余的耦合。
    """

    def __init__(self) -> None:
        self.finished: list[tuple[str, str, bool]] = []

    def __getattr__(self, _name):
        def _noop(*_args, **_kwargs):
            return None

        return _noop

    def finish(self, title, detail, *, ok=True) -> None:
        self.finished.append((title, detail, ok))


def _finish_dialog(qtbot, temp_data_dir, n_items: int = 2):
    mm = ModManager()
    mm.set_game("game_a")
    char = mm.add_character("角色", node_type="character")
    dlg = ImportProgressDialog([("", "A")] * n_items, char.id, mm)
    qtbot.addWidget(dlg)
    view = _RecordingView()
    dlg._view = view
    # 本测试只验证完成状态反馈，使用空操作替代文件清理和保留文件名查询，
    # 避免访问测试替身未提供的 mod.path。
    dlg._completed_keep_names = lambda: set()
    dlg._cleanup_orphaned_files = lambda *_a, **_k: None
    dlg._cleanup_uncommitted_preview_caches = lambda *_a, **_k: None
    return dlg, view


def test_import_top_level_reports_failure_when_any_item_failed(qtbot, temp_data_dir):
    """明细存在失败项时，顶层通知必须显示失败状态。"""
    dlg, view = _finish_dialog(qtbot, temp_data_dir)
    dlg._worker = _FakeWorker()

    # 一成一败：prepare 抛出的异常经 file_prepared 送到主线程，即损坏 .d3dxmod 的形态。
    dlg._register_mod_in_db = lambda result: object()
    dlg._on_file_prepared(0, object.__new__(PreparedIntake))
    dlg._register_mod_in_db = lambda result: None
    dlg._on_file_prepared(1, RuntimeError("marker 解析失败"))

    dlg._on_finished()

    assert view.finished, "终态必须落到面板上"
    title, detail, ok = view.finished[-1]
    assert ok is False, f"有失败项时顶层不得为成功态: {title!r}"
    assert "失败" in title
    assert "1 个失败" in detail
    assert "已导入 1 个" in detail


def test_import_top_level_stays_success_without_failures(qtbot, temp_data_dir):
    dlg, view = _finish_dialog(qtbot, temp_data_dir, n_items=1)
    dlg._worker = _FakeWorker()
    dlg._register_mod_in_db = lambda result: object()
    dlg._on_file_prepared(0, object.__new__(PreparedIntake))

    dlg._on_finished()

    title, _detail, ok = view.finished[-1]
    assert ok is True
    assert title == "导入完成"


def test_duplicate_only_import_is_not_reported_as_failure(qtbot, temp_data_dir):
    from src.core.mod_intake import DuplicateIntakeError

    dlg, view = _finish_dialog(qtbot, temp_data_dir, n_items=1)
    dlg._worker = _FakeWorker()
    dlg._on_file_prepared(0, DuplicateIntakeError("same hash"))

    dlg._on_finished()

    _title, detail, ok = view.finished[-1]
    assert ok is True
    assert "重复" in detail
    assert "失败" not in detail
