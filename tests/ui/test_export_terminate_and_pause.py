"""验证导出暂停、终止选择和已确认产物保留。"""

from __future__ import annotations

import threading
import time

import pytest
from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QWidget

import src.ui.dialogs.export_progress as ep
from src.core.mod_exporter import ExportItem
from src.ui.dialogs.export_progress import (
    ExportProgressDialog,
    ExportWorkerThread,
    _ExportAborted,
)
from src.ui.widgets.task_center import ST_DONE, TaskCenterPanel

pytestmark = pytest.mark.usefixtures("temp_data_dir")


class _StubManager:
    def __init__(self, root):
        self._root = root

    def _get_library_root(self):
        return self._root


class _FakeSignal:
    def connect(self, *a, **k):
        pass

    def disconnect(self, *a, **k):
        pass


class _FakeWorker:
    def __init__(self, *a, **k):
        self.progress_updated = _FakeSignal()
        self.item_done = _FakeSignal()
        self.finished_all = _FakeSignal()
        self.stage_progress = _FakeSignal()
        self.finished = _FakeSignal()
        self.paused = False
        self.terminated = False

    def start(self):
        pass

    def isRunning(self):
        return True

    def pause(self):
        self.paused = True

    def resume(self):
        self.paused = False

    def terminate_export(self):
        self.terminated = True

    @property
    def is_paused(self):
        return self.paused


def _worker(qtbot, tmp_path) -> ExportWorkerThread:
    return ExportWorkerThread([], str(tmp_path / "out"), "zip", True, str(tmp_path / "lib"))


def _wait_until(pred, timeout: float) -> bool:
    """轮询等条件成立；超时返回 False。

    每轮调用 processEvents，处理工作线程通过队列连接投递到主线程的 item_done 信号。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QCoreApplication.processEvents()
        if pred():
            return True
        time.sleep(0.01)
    QCoreApplication.processEvents()
    return bool(pred())


# 结果计数必须等于实际保留的产物数量。


def _items(tmp_path, n: int) -> list[ExportItem]:
    out = []
    for i in range(n):
        src = tmp_path / f"src{i}.zip"
        src.write_text(f"body{i}", encoding="utf-8")
        out.append(ExportItem(name=f"M{i}", src_path=str(src)))
    return out


def test_item_finished_at_the_same_moment_as_terminate_is_still_counted(
    qtbot,
    tmp_path,
):
    """终止后仅保留已确认导出的产物，使明细、磁盘文件和结果计数一致。

    未经过确认的已完成文件须撤回；缺少内部取消检查点的复制也在完成后清理。
    """
    items = _items(tmp_path, 3)
    dest = tmp_path / "out"
    worker = ExportWorkerThread(items, str(dest), "zip", True, str(tmp_path / "lib"))

    done: list[int] = []
    worker.item_done.connect(lambda i, _r: done.append(i))

    class _Exporter:
        """第 2 项压完的同一刻用户点了终止（压完 → 才 terminate）。"""

        def export_one(self, item, *a, **k):
            dest.mkdir(parents=True, exist_ok=True)
            out_path = dest / f"{item.name}.zip"
            out_path.write_text("done", encoding="utf-8")
            if item.name == "M1":
                worker.terminate_export()
            return ep.ExportResult(True, item.name, out_path=str(out_path))

    worker._exporter = _Exporter()
    worker.run()

    produced = sorted(p.name for p in dest.glob("*.zip"))
    assert produced == ["M0.zip"], "未确认的产物必须撤回，不许点了终止还多出一个"
    assert done == [0], "撤回的那一项不计数"
    assert len(done) == len(produced), "上报条数必须等于磁盘产物数"


def test_commit_gate_holds_finished_item_until_user_decides(qtbot, tmp_path):
    """暂停期间暂缓上报已执行完毕的当前项，使确认弹窗中的计数与已确认结果一致。

    继续导出后才将当前项计入结果；选择终止时撤回其产物。
    """
    items = _items(tmp_path, 2)
    dest = tmp_path / "out"
    worker = ExportWorkerThread(items, str(dest), "zip", True, str(tmp_path / "lib"))

    done: list[int] = []
    worker.item_done.connect(lambda i, _r: done.append(i))
    reached_gate = threading.Event()

    class _Exporter:
        def export_one(self, item, *a, **k):
            dest.mkdir(parents=True, exist_ok=True)
            out_path = dest / f"{item.name}.zip"
            out_path.write_text("done", encoding="utf-8")
            if item.name == "M0":
                worker.pause()  # 模拟「这一项刚跑完，用户按了 ■」
                reached_gate.set()
            return ep.ExportResult(True, item.name, out_path=str(out_path))

    worker._exporter = _Exporter()
    t = threading.Thread(target=worker.run, daemon=True)
    t.start()

    assert reached_gate.wait(2.0)
    # 直连信号在 worker 线程里同步跑，故 done 可直接观察
    assert not _wait_until(lambda: done, 0.3), "跑完的那一项在用户决定前不许上报（否则弹窗数字和最终结论又对不上）"
    assert (dest / "M0.zip").exists(), "闸门只拦上报，不该提前删产物"

    worker.resume()  # 用户选「继续导出」
    assert _wait_until(lambda: len(done) == 2, 2.0), "放行后必须补上并继续下一项"
    t.join(timeout=2.0)

    produced = sorted(p.name for p in dest.glob("*.zip"))
    assert produced == ["M0.zip", "M1.zip"]
    assert len(done) == len(produced)


class _RealisticExporter:
    """模拟导出器忽略进度回调异常并继续写入产物的行为。

    保留 `ModExporter.export_one` 的异常处理约束，验证工作线程在终止后撤回当前项产物，
    而不能仅依赖 `_ExportAborted` 从进度回调向外传播。
    """

    def __init__(self, worker, dest, terminate_on: str = ""):
        self._worker = worker
        self._dest = dest
        self._terminate_on = terminate_on

    def export_one(self, item, *a, **k):
        def _stage(frac, text):
            try:
                k["stage_cb"](frac, text)
            except Exception:
                pass

        _stage(0.05, "开始")
        if item.name == self._terminate_on:
            self._worker.terminate_export()
        _stage(0.50, "压缩中")  # 导出取消异常应在工作线程边界内处理。
        _stage(0.90, "收尾")
        self._dest.mkdir(parents=True, exist_ok=True)
        out_path = self._dest / f"{item.name}.zip"
        out_path.write_text("done", encoding="utf-8")  # 忽略进度回调异常后继续写入产物。
        return ep.ExportResult(True, item.name, out_path=str(out_path))


def test_item_cut_mid_flight_has_its_product_withdrawn(qtbot, tmp_path):
    """请求终止后，即使当前项继续完成并写入磁盘，也须撤回其产物且不计入结果。"""
    items = _items(tmp_path, 3)
    dest = tmp_path / "out"
    worker = ExportWorkerThread(items, str(dest), "zip", True, str(tmp_path / "lib"))

    done: list[int] = []
    worker.item_done.connect(lambda i, _r: done.append(i))
    worker._exporter = _RealisticExporter(worker, dest, terminate_on="M1")
    worker.run()

    produced = sorted(p.name for p in dest.glob("*.zip"))
    assert produced == ["M0.zip"], "被掐断那一项的产物必须撤回（它跑完了也不算，因为终止已经按下）"
    assert done == [0], "撤回的那一项不许计数"
    assert len(done) == len(produced), "上报条数必须等于磁盘产物数"


def test_unsettled_rows_are_marked_not_exported_on_finish(qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr(ep, "ExportWorkerThread", _FakeWorker)
    out = tmp_path / "out"
    out.mkdir()

    marked: list[tuple[int, str, str]] = []

    class _Panel:
        pause_requested = _FakeSignal()
        resume_requested = _FakeSignal()
        stop_requested = _FakeSignal()

        def start_batch(self, *a, **k):
            pass

        def update_progress(self, *a, **k):
            pass

        def update_item(self, index, name, status, detail=""):
            marked.append((index, status, detail))

        def finish(self, *a, **k):
            pass

        def finish_cancelled(self, *a, **k):
            pass

    dlg = ExportProgressDialog(
        [],
        str(out),
        _StubManager(tmp_path / "lib"),
        export_items=[ExportItem(name=f"x{i}", src_path=str(tmp_path / f"x{i}")) for i in range(4)],
        panel=_Panel(),
    )
    qtbot.addWidget(dlg)
    dlg._settled = {0, 1}  # 只有前两项上报过终态
    marked.clear()

    dlg._finish_panel(cancelled=True)

    assert [(i, d) for i, _s, d in marked] == [(2, "未导出"), (3, "未导出")]


def test_export_panel_wording_never_says_intake(qtbot, tmp_path, monkeypatch):
    """共享任务面板用于导出时，按钮、状态和明细均须使用导出文案。"""
    monkeypatch.setattr(ep, "ExportWorkerThread", _FakeWorker)
    host = QWidget()
    host.resize(1200, 700)
    qtbot.addWidget(host)
    host.show()
    panel = TaskCenterPanel(host)
    out = tmp_path / "out"
    out.mkdir()

    dlg = ExportProgressDialog(
        [],
        str(out),
        _StubManager(tmp_path / "lib"),
        export_items=[ExportItem(name="x0", src_path=str(tmp_path / "x0"))],
        panel=panel,
    )
    qtbot.addWidget(dlg)

    assert "纳管" not in panel._stop_btn.toolTip()
    assert "导出" in panel._stop_btn.toolTip()

    # 明细行不传 detail 时会落到默认文案上
    panel.update_item(0, "x0", ST_DONE)
    assert "纳管" not in panel._items[0]._state_label.text()

    panel.finish_cancelled("成功 0")
    assert "纳管" not in panel._title.full_text()
    assert "已停止导出" == panel._title.full_text()


def test_intake_wording_stays_the_default_for_other_callers(qtbot):
    host = QWidget()
    host.resize(900, 600)
    qtbot.addWidget(host)
    host.show()
    panel = TaskCenterPanel(host)

    panel.start_batch(2, "正在纳管 2 个新增项")

    assert panel._stop_btn.toolTip() == "停止纳管并恢复原文件"
    panel.update_item(0, "a", ST_DONE)
    assert panel._items[0]._state_label.text() == "已纳管"
    panel.finish_cancelled("成功 0")
    assert panel._title.full_text() == "已停止纳管"


def test_summary_counts_always_add_up_to_total(qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr(ep, "ExportWorkerThread", _FakeWorker)
    out = tmp_path / "out"
    out.mkdir()
    dlg = ExportProgressDialog(
        [],
        str(out),
        _StubManager(tmp_path / "lib"),
        export_items=[ExportItem(name=f"x{i}", src_path=str(tmp_path / f"x{i}")) for i in range(5)],
    )
    qtbot.addWidget(dlg)
    dlg._success, dlg._failed, dlg._skipped = 2, 1, 0  # 共 5 项，其中 2 项尚未执行。

    detail = dlg._summary_detail()

    assert "成功 2" in detail
    assert "失败 1" in detail
    assert "未导出 2" in detail, "被终止而没跑的那些必须在结论里露出来"


# 暂停应在进度回调边界生效。


def test_pause_blocks_inside_the_stage_callback(qtbot, tmp_path):
    """暂停在归档处理的进度回调边界生效。"""
    worker = _worker(qtbot, tmp_path)
    worker.pause()

    returned = threading.Event()

    def _call():
        worker._emit_stage(0, 3, 0.5, "压缩中")
        returned.set()

    t = threading.Thread(target=_call, daemon=True)
    t.start()
    assert not returned.wait(0.3), "暂停后 stage 回调必须阻塞，否则暂停不会立即生效"

    worker.resume()
    assert returned.wait(2.0), "继续后 stage 回调必须放行"
    t.join(timeout=2.0)


def test_stage_callback_raises_abort_after_terminate(qtbot, tmp_path):
    worker = _worker(qtbot, tmp_path)
    worker.terminate_export()

    with pytest.raises(_ExportAborted):
        worker._emit_stage(0, 3, 0.5, "压缩中")


def test_terminate_releases_a_paused_worker(qtbot, tmp_path):
    worker = _worker(qtbot, tmp_path)
    worker.pause()
    assert worker.is_paused is True

    worker.terminate_export()

    assert worker.is_paused is False, "terminate_export 必须 set() 解除暂停阻塞"
    with pytest.raises(_ExportAborted):
        worker._emit_stage(0, 1, 0.1, "x")


# 验证终止选项及完成后打开目标目录的行为。


@pytest.fixture()
def controller(qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr(ep, "ExportWorkerThread", _FakeWorker)
    out = tmp_path / "out"
    out.mkdir()
    dlg = ExportProgressDialog(
        [],
        str(out),
        _StubManager(tmp_path / "lib"),
        export_items=[ExportItem(name="x", src_path=str(tmp_path / "x"))],
    )
    qtbot.addWidget(dlg)
    dlg.start_export()
    yield dlg, out


def test_purge_removes_only_the_products_this_batch_wrote(controller):
    dlg, out = controller
    mine_file = out / "mine.zip"
    mine_file.write_text("z", encoding="utf-8")
    mine_dir = out / "mine_folder"
    mine_dir.mkdir()
    (mine_dir / "a.ini").write_text("a", encoding="utf-8")
    stranger = out / "别人的.zip"  # 目标目录里本来就有的东西
    stranger.write_text("keep", encoding="utf-8")

    dlg._exported_paths = [str(mine_file), str(mine_dir)]
    dlg._success = 2

    dlg._purge_exported()

    assert not mine_file.exists()
    assert not mine_dir.exists()
    assert stranger.exists(), "只许删本批 out_path 记下的产物，绝不扫目标目录"
    assert dlg._success == 0
    assert dlg._exported_paths == []


def test_finish_opens_dest_dir_by_default(controller, monkeypatch):
    dlg, _out = controller
    opened: list[int] = []
    monkeypatch.setattr(dlg, "_open_dest_dir", lambda: opened.append(1))
    dlg._success = 2

    dlg._finish_panel(cancelled=False)

    assert opened == [1], "正常导出完成后应自动打开导出目录"


def test_finish_does_not_open_dir_after_purge(controller, monkeypatch):
    dlg, _out = controller
    opened: list[int] = []
    monkeypatch.setattr(dlg, "_open_dest_dir", lambda: opened.append(1))
    dlg._success = 2
    dlg._open_dir_on_finish = False  # 「取消全部导出」置的

    dlg._finish_panel(cancelled=True)

    assert opened == [], "选了取消全部导出就不该再弹目录"


def test_finish_does_not_open_dir_when_nothing_was_exported(controller, monkeypatch):
    dlg, _out = controller
    opened: list[int] = []
    monkeypatch.setattr(dlg, "_open_dest_dir", lambda: opened.append(1))
    dlg._success = 0

    dlg._finish_panel(cancelled=False)

    assert opened == [], "一个都没导出成功时弹目录等于给用户看空文件夹"


def test_terminate_asks_before_stopping_and_can_be_waved_off(controller, monkeypatch):
    dlg, _out = controller
    worker = dlg._worker
    order: list[str] = []

    real_pause = worker.pause

    def _spy_pause():
        order.append("pause")
        real_pause()

    worker.pause = _spy_pause

    class _Box:
        """假 QMessageBox：exec 时记录「问的时候 worker 是否已暂停」。"""

        instance = None
        pick = "resume"

        def __init__(self, *a, **k):
            self._buttons: dict[str, object] = {}
            _Box.instance = self

        def setWindowTitle(self, *a):
            pass

        def setIcon(self, *a):
            pass

        def setText(self, text):
            self.text = text

        def setDefaultButton(self, *a):
            pass

        def addButton(self, label, _role):
            self._buttons[label] = object()
            return self._buttons[label]

        def exec(self):
            order.append("ask")

        def clickedButton(self):
            return self._buttons[
                {
                    "resume": "继续导出",
                    "keep": "保留已导出的Mod",
                    "purge": "取消全部导出",
                }[_Box.pick]
            ]

    monkeypatch.setattr(ep, "QMessageBox", _Box)
    _Box.Icon = type("I", (), {"Question": 0})
    _Box.ButtonRole = type("R", (), {"AcceptRole": 0, "DestructiveRole": 1, "RejectRole": 2})

    dlg._success, dlg._failed, dlg._skipped, dlg._total = 2, 0, 0, 5
    _Box.pick = "resume"
    dlg._on_terminate()

    assert order == ["pause", "ask"], "必须先按住 worker 再弹窗，顺序不能反"
    assert "已导出 2 个 Mod" in _Box.instance.text
    assert "还有 3 个 Mod 未导出" in _Box.instance.text
    assert dlg._was_terminated is False, "选「继续导出」不该终止"
    assert worker.terminated is False
    assert worker.paused is False, "选「继续导出」要解冻放行"


def test_terminate_purge_choice_rolls_back_products(controller, monkeypatch):
    dlg, _out = controller
    purged: list[int] = []
    monkeypatch.setattr(dlg, "_purge_exported", lambda: purged.append(1))
    monkeypatch.setattr(dlg, "_finish_panel", lambda **k: None)

    dlg._purge_on_terminate = True
    dlg._do_terminate_finish()

    assert purged == [1]


def test_terminate_keep_choice_does_not_roll_back(controller, monkeypatch):
    dlg, _out = controller
    purged: list[int] = []
    monkeypatch.setattr(dlg, "_purge_exported", lambda: purged.append(1))
    monkeypatch.setattr(dlg, "_finish_panel", lambda **k: None)

    dlg._purge_on_terminate = False
    dlg._do_terminate_finish()

    assert purged == []
