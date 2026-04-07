from src.services.gamebanana.downloader import GbDownloadManager
from src.ui.widgets.gb_download_flyout import GbDownloadFlyout


def test_global_filter_only_observes_while_visible(qtbot, tmp_path, monkeypatch):
    from PySide6.QtCore import QCoreApplication, QEvent, QObject

    class ObservedFlyout(GbDownloadFlyout):
        calls = 0

        def eventFilter(self, watched, event):
            if event.type() == QEvent.Type.User:
                self.calls += 1
            return super().eventFilter(watched, event)

    mgr = GbDownloadManager(base_dir=tmp_path)
    try:
        flyout = ObservedFlyout(mgr)
        qtbot.addWidget(flyout)
        monkeypatch.setattr(flyout, "_ensure_acrylic", lambda: None)
        unrelated = QObject()

        def send():
            before = flyout.calls
            QCoreApplication.sendEvent(unrelated, QEvent(QEvent.Type.User))
            return flyout.calls - before

        assert send() == 0
        for _ in range(2):
            flyout.show()
            assert send() == 1
            flyout.hide()
            assert send() == 0
        flyout.show()
        flyout.close()
        assert send() == 0
        flyout.show()
        assert send() == 1
    finally:
        mgr.shutdown()


def test_flyout_constructs(qtbot, tmp_path):
    mgr = GbDownloadManager(base_dir=tmp_path)
    try:
        f = GbDownloadFlyout(mgr)
        qtbot.addWidget(f)
        assert len(f._row_widgets) == len(mgr.active())
    finally:
        mgr.shutdown()


def test_clear_completed_button_present(qtbot, tmp_path):
    mgr = GbDownloadManager(base_dir=tmp_path)
    try:
        f = GbDownloadFlyout(mgr)
        qtbot.addWidget(f)
        # 头部清理按钮（Edge 风格）：清除已完成 / 全部清除。
        assert f.btn_clear_done is not None
        assert f.btn_clear_all is not None
    finally:
        mgr.shutdown()


def test_retry_first_calls_manager_when_failed(qtbot, tmp_path, monkeypatch):
    mgr = GbDownloadManager(base_dir=tmp_path)
    try:
        f = GbDownloadFlyout(mgr)
        qtbot.addWidget(f)
        # 在管理器中构造失败任务。
        from pathlib import Path

        from src.services.gamebanana.downloader import GbDownloadTask
        from src.services.gamebanana.models import GbAuthor, GbModel, GbModFile, GbModProfile

        profile = GbModProfile(
            mod_id=1,
            model=GbModel.MOD,
            name="x",
            submitter=GbAuthor(id=1, name="A", avatar_url="", profile_url=""),
            thumbnail_url="",
            page_url="",
            view_count=0,
            like_count=0,
            download_count=0,
            comment_count=0,
            is_nsfw=False,
            date_updated_ts=0,
        )
        file = GbModFile(
            file_id=1,
            file_name="x.zip",
            description="",
            download_url="https://x",
            size_bytes=1,
            date_added_ts=0,
            md5_checksum="m",
            av_result="",
            version="",
        )
        task = GbDownloadTask(profile, file, Path(tmp_path / "x.zip"), game_id="game_a")
        task._set_state("failed")
        mgr._tasks[1] = task
        f._refresh()  # 重建行：失败任务那一行主按钮即「重试」
        called = []
        monkeypatch.setattr(mgr, "retry", lambda fid: called.append(fid))
        # 通过下载行的主按钮触发 manager.retry，验证重试事件的参数。
        f._row_widgets[1]._on_retry()
        assert called == [1]
    finally:
        mgr.shutdown()


def test_progress_after_refresh_does_not_raise(qtbot, tmp_path):
    """队列刷新销毁下载行后，迟到的进度信号不得访问已删除控件。

    刷新时需解除旧连接，并忽略不再属于当前行的通知。
    """
    from pathlib import Path

    from src.services.gamebanana.downloader import GbDownloadTask
    from src.services.gamebanana.models import GbAuthor, GbModel, GbModFile, GbModProfile

    mgr = GbDownloadManager(base_dir=tmp_path)
    try:
        f = GbDownloadFlyout(mgr)
        qtbot.addWidget(f)

        profile = GbModProfile(
            mod_id=1,
            model=GbModel.MOD,
            name="x",
            submitter=GbAuthor(id=1, name="A", avatar_url="", profile_url=""),
            thumbnail_url="",
            page_url="",
            view_count=0,
            like_count=0,
            download_count=0,
            comment_count=0,
            is_nsfw=False,
            date_updated_ts=0,
        )
        file = GbModFile(
            file_id=1,
            file_name="a.zip",
            description="",
            download_url="https://x",
            size_bytes=1,
            date_added_ts=0,
            md5_checksum="m",
            av_result="",
            version="",
        )
        task = GbDownloadTask(profile, file, Path(tmp_path / "a.zip"), game_id="game_a")
        mgr._tasks[1] = task
        mgr.queue_changed.emit()  # 触发列表刷新并绑定下载行信号。

        # 修改队列并再次刷新，使旧进度条被销毁。
        mgr._tasks.clear()
        mgr.queue_changed.emit()

        # 迟到的进度信号不得抛出异常。
        task.progress.emit(50, 100)
    finally:
        mgr.shutdown()
