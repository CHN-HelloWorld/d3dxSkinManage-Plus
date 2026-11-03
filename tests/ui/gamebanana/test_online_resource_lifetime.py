"""验证在线菜单与描述切换不会累积已退出的控件或图像资源。"""

from __future__ import annotations

import gc
import threading
import weakref

import httpx
import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPoint, Qt, QTimer, QUrl
from PySide6.QtGui import QImage, QPixmap, QTextDocument
from PySide6.QtWidgets import QApplication, QLabel, QMenu, QWidget

from src.services.gamebanana.downloader import GbDownloadManager, GbDownloadTask
from src.services.gamebanana.models import GbModFile, GbModProfile
from src.ui.pages.gamebanana_store import GameBananaStorePage
from src.ui.widgets.gb_download_flyout import _DownloadRow
from src.ui.widgets.gb_html_view import GbHtmlView
from src.ui.widgets.preview import ImageViewerDialog


def _flush_deletes() -> None:
    """投递 Qt 事件并显式销毁延迟对象，排除事件队列延迟产生的假阳性。"""
    QCoreApplication.processEvents()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    QCoreApplication.processEvents()
    gc.collect()


@pytest.mark.parametrize("entrypoint", ["context", "button"])
def test_download_row_reuses_a_bounded_menu(qtbot, temp_data_dir, tmp_path, entrypoint):
    """反复打开同一行的两个菜单入口后，至多保留一个可复用的菜单窗口。"""
    manager = GbDownloadManager(base_dir=tmp_path / "downloads")
    file = GbModFile(101, "sample.zip", "", "https://example.invalid/sample.zip", 4, 0, "", "")
    task = GbDownloadTask(GbModProfile.from_api({"_idRow": 201}), file, tmp_path / "sample.zip", game_id="sample")
    row = _DownloadRow(manager, task)
    qtbot.addWidget(row)
    row.show()
    dismiss = QTimer(row)
    dismiss.setInterval(10)

    def close_menu():
        """仅在菜单实际显示后发送 Escape，保留原生模态循环及销毁行为。"""
        menu = QApplication.activePopupWidget()
        if isinstance(menu, QMenu):
            dismiss.stop()
            qtbot.keyClick(menu, Qt.Key.Key_Escape)

    dismiss.timeout.connect(close_menu)
    try:
        for _ in range(4):
            dismiss.start()
            if entrypoint == "context":
                row._on_context_menu(QPoint())
            else:
                row._show_menu_at_button()
            _flush_deletes()
        assert len(row.findChildren(QMenu)) <= 1
    finally:
        dismiss.stop()
        manager.shutdown()


class _ImmediateImages:
    """同步提供小型合成图像，仅隔离网络输入而保留控件和文档行为。"""

    def load(self, url, target, **kwargs):
        """每次交付独立像素数据，使遗留 QLabel 对图像的持有可被观察。"""
        pixmap = QPixmap(128, 128)
        pixmap.fill(Qt.GlobalColor.red)
        target.setPixmap(pixmap)

    def cancel_for(self, target):
        """同步加载没有在途请求，但保留与真实加载器一致的取消接口。"""


def test_html_switch_releases_old_pixels_and_bounds_hidden_targets(qtbot, temp_data_dir):
    view = GbHtmlView(image_loader=_ImmediateImages())
    qtbot.addWidget(view)
    for index in range(4):
        url = f"https://example.invalid/image-{index}.png"
        view.set_html(f'<p><img src="{url}"></p>')
        view._collect_image(url)
        view._rerender_with_images()
    view.set_html("<p>empty</p>")
    _flush_deletes()
    targets = view.findChildren(QLabel)
    assert len(targets) <= 1
    assert all(target.pixmap().isNull() for target in targets)
    assert view._img_resources == {}
    for index in range(4):
        resource = view.document().resource(
            QTextDocument.ResourceType.ImageResource,
            QUrl(f"https://example.invalid/image-{index}.png"),
        )
        assert not isinstance(resource, (QImage, QPixmap)) or resource.isNull()


def test_online_image_viewer_does_not_accumulate_closed_dialogs(qtbot, temp_data_dir, tmp_path, monkeypatch):
    """真实查看器重复关闭后，在线页面对其窗口持有数量必须保持有限。"""

    class Host(QWidget):
        """直接复用商店的打开入口，避免加载无关在线页面状态。"""

        _open_image_viewer = GameBananaStorePage._open_image_viewer

    host = Host()
    qtbot.addWidget(host)
    image = QPixmap(64, 64)
    image.fill(Qt.GlobalColor.blue)
    path = tmp_path / "preview.png"
    assert image.save(str(path))
    original_exec = ImageViewerDialog.exec
    opened = []

    def close_dialog(dialog):
        """保留模态循环与关闭语义，只安排一次自动拒绝。"""
        opened.append(weakref.ref(dialog))
        QTimer.singleShot(0, dialog.reject)
        return original_exec(dialog)

    monkeypatch.setattr(ImageViewerDialog, "exec", close_dialog)
    for _ in range(4):
        host._open_image_viewer(path)
        _flush_deletes()
    assert len(opened) == 4
    assert len(host.findChildren(ImageViewerDialog)) <= 1


def test_store_download_callbacks_release_removed_tasks(
    qtbot, temp_data_dir, fake_gb_client, fake_image_loader, respx_mock, monkeypatch
):
    """商店冻结的安装回调不得让已移除任务与旧详情形成永久强引用环。"""
    page = GameBananaStorePage(client=fake_gb_client, image_loader=fake_image_loader)
    qtbot.addWidget(page)
    manager = page._download_manager
    page._local_game_name = "sample"
    installed = []
    release = threading.Event()
    references = []
    monkeypatch.setattr(page, "_show_download_flyout", lambda: None)
    monkeypatch.setattr(page, "_after_download", lambda file, *args: installed.append(file.file_id))

    def response(request):
        """在商店完成信号接线后才交付响应，避免复现受线程调度速度影响。"""
        assert release.wait(3)
        return httpx.Response(200, content=b"data")

    try:
        for index in range(4):
            release.clear()
            url = f"https://example.invalid/ui-archive-{index}.zip"
            respx_mock.get(url).mock(side_effect=response)
            profile = GbModProfile.from_api(
                {
                    "_idRow": 201,
                    "_sName": "sample",
                    "_aFiles": [{"_idRow": 101 + index, "_sFile": f"sample-{index}.zip", "_sDownloadUrl": url}],
                }
            )
            page.detail_view._profile = profile
            page._on_download_requested(profile.files[0])
            task = manager.get_task(101 + index)
            assert task is not None
            release.set()
            qtbot.waitUntil(lambda item=task: len(installed) == index + 1 and not item.is_running, timeout=3000)
            references.append((weakref.ref(task), weakref.ref(profile)))
            manager.delete_task(task.file.file_id)
            page.detail_view._profile = None
            del task, profile
            _flush_deletes()
        assert all(task() is None and profile() is None for task, profile in references)
    finally:
        release.set()
        page.shutdown()
        manager.shutdown()
