"""验证下载归档在安装交接中的占用、名称与来源元数据。"""

from __future__ import annotations

import hashlib
import json
import zipfile
from dataclasses import replace
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QMainWindow

from src.core.config import Config
from src.core.mod_intake import ModIntakeService
from src.core.mod_manager import ModManager
from src.core.mod_package import MANIFEST_KIND, read_sidecar
from src.services.gamebanana.downloader import GbDownloadManager
from src.services.gamebanana.models import GbAuthor, GbInstallMetadata, GbModel, GbModFile, GbModProfile
from src.services.gamebanana.pending_install_store import PendingInstall, PendingInstallStore
from src.ui.main_window import MainWindow
from src.ui.pages.gamebanana_store import GameBananaStorePage
from src.ui.widgets.gb_download_flyout import GbDownloadFlyout
from src.ui.widgets.gb_install_target_dialog import GbInstallTargetDialog
from src.utils.paths import get_gb_download_dir
from tests.core.test_mod_update_transaction import _archive
from tests.helpers import register_games, reset_game_service
from tests.services.gamebanana.test_download_consumers import _profile
from tests.services.gamebanana.test_download_consumers import manager as manager


class ImportHost(MainWindow):
    """使用真实导入控制器和文件操作，仅禁用无关页面刷新。"""

    def __init__(self, manager, page):
        """连接真实商店安装入口，并初始化后台导入所需的宿主状态。"""
        QMainWindow.__init__(self)
        self._config = Config()
        self._mod_manager = manager
        self._game_service = manager._game_service
        self._gb_store_page = page
        self._pending_gb_installs = []
        self._import_active = True
        self._import_controller = None
        self._intake_progress = None
        self._watcher_debounce = SimpleNamespace(stop=lambda: None)
        page.mod_install_requested.connect(self._on_gb_install_requested)

    def _suppress_watcher(self):
        """隔离宿主没有启动的文件监视器。"""

    def _defer_source_reconcile(self):
        """避免测试导入完成后创建无关的库对账任务。"""

    def _reload_overview_level(self, game_id):
        """隔离宿主没有创建的概览页面。"""

    def closeEvent(self, event):
        """仅关闭测试窗口，后台工作由夹具负责回收。"""
        QMainWindow.closeEvent(self, event)


@pytest.fixture
def handoff(qtbot, qapp, temp_data_dir, fake_gb_client, fake_image_loader):
    """提供真实缓存归档和安装宿主，结束时停止后台任务与文件监视器。"""
    register_games("game-a", "game-b")
    manager = ModManager()
    manager.set_game("game-a")
    manager.add_character(name="Character", node_type="character")
    data = _archive(temp_data_dir / "source.zip", b"shared archive body").read_bytes()
    md5 = hashlib.md5(data).hexdigest()
    archive = get_gb_download_dir() / f"{md5}__11__skin.zip"
    archive.parent.mkdir(parents=True, exist_ok=True)
    archive.write_bytes(data)
    dm = GbDownloadManager()
    profile = GbModProfile(
        mod_id=123,
        model=GbModel.MOD,
        name="Character skin",
        submitter=GbAuthor(id=1, name="Author", avatar_url="", profile_url=""),
        thumbnail_url="",
        page_url="https://gamebanana.com/mods/123",
        view_count=0,
        like_count=0,
        download_count=0,
        comment_count=0,
        is_nsfw=False,
        date_updated_ts=0,
        category_name="Character",
        super_category_id=1,
    )
    file = GbModFile(
        file_id=11,
        file_name="skin.zip",
        description="",
        download_url="https://example.test/skin.zip",
        size_bytes=len(data),
        date_added_ts=2000,
        md5_checksum=md5,
        av_result="clean",
        version="v1",
    )
    pages, hosts = [], []

    def page():
        """创建共享下载管理器的商店页面，并登记关闭清理。"""
        result = GameBananaStorePage(
            client=fake_gb_client,
            image_loader=fake_image_loader,
            mod_index=manager,
            download_manager=dm,
        )
        pages.append(result)
        qtbot.addWidget(result, before_close_func=lambda widget: widget.shutdown())
        return result

    def host(store):
        """创建连接商店信号的导入宿主，由夹具统一回收工作线程。"""
        result = ImportHost(manager, store)
        hosts.append(result)
        qtbot.addWidget(result)
        return result

    yield SimpleNamespace(manager=manager, dm=dm, archive=archive, file=file, profile=profile, page=page, host=host)
    for main in hosts:
        controller = main._import_controller
        if controller is not None and controller._worker is not None:
            controller._worker.terminate_import()
            controller._worker.wait(5000)
    qapp.processEvents()
    for store in pages:
        store.shutdown()
    dm.shutdown()
    manager.stop_ini_sync()
    manager.variants.stop_watch()
    reset_game_service()


@pytest.mark.parametrize("install_path", ["async", "synchronous", "restored"])
def test_store_install_uses_original_filename_for_persisted_mod_and_container(handoff, qtbot, install_path):
    """即时、独立与重启恢复的安装均使用原始文件名，并保持归档内容和来源证据。"""
    case = handoff
    original = case.archive.read_bytes()
    if install_path == "restored":
        PendingInstallStore().add(PendingInstall.capture("game-a", case.profile, case.file, case.archive))
        case.file = replace(case.file, file_name="changed.zip")
        case.profile.name = "Changed title"
    page = case.page()
    if install_path == "async":
        host = case.host(page)
        host._import_active = False
        page._after_download(case.file, case.archive, case.profile)
        qtbot.waitUntil(lambda: not host._import_active, timeout=15000)
    elif install_path == "restored":
        page._drain_pending_installs("game-a")
        assert not PendingInstallStore().pending_game_ids()
    else:
        page._after_download(case.file, case.archive, case.profile)

    mods = case.manager.get_mods()
    assert len(mods) == 1
    mod = mods[0]
    assert mod.name == "skin"
    assert mod.folder_name == "skin"
    body = Path(mod.path)
    assert body.parent.name == "skin"
    assert body.name == "skin.zip"
    assert body.read_bytes() == original
    assert read_sidecar(body)["name"] == "skin"
    assert case.manager.get_mod(mod.mod_id).name == "skin"
    metadata = mod.info.custom_data["gamebanana"]
    assert metadata["mod_id"] == 123
    assert metadata["file_id"] == 11
    assert metadata["file_md5"] == hashlib.md5(original).hexdigest()
    assert metadata["page_url"] == "https://gamebanana.com/mods/123"
    assert mod.info.version == "v1"


def test_shared_download_keeps_envelope_name_above_original_filename(handoff, qtbot):
    """缓存文件名仅提供容器默认名，异步安装仍采用分享包的正式显示名称。"""
    case = handoff
    manifest = {"kind": MANIFEST_KIND, "schema": 2, "name": "Formal package", "info": {}, "previews": []}
    with zipfile.ZipFile(case.archive, "a") as archive:
        archive.writestr(".d3dxmeta/info.json", json.dumps(manifest))
    original = case.archive.read_bytes()
    case.file = replace(case.file, md5_checksum=hashlib.md5(original).hexdigest(), size_bytes=len(original))
    case.archive = case.archive.rename(case.archive.with_name(f"{case.file.md5_checksum}__11__skin.zip"))
    case.dm.refresh_local_index()
    page = case.page()
    host = case.host(page)
    host._import_active = False
    page._after_download(case.file, case.archive, case.profile)
    qtbot.waitUntil(lambda: not host._import_active, timeout=15000)

    mods = case.manager.get_mods()
    assert len(mods) == 1
    mod = mods[0]
    assert mod.name == "Formal package"
    assert read_sidecar(mod.path)["name"] == "Formal package"
    assert mod.folder_name == "skin"
    assert Path(mod.path).parent.name == "skin"
    assert mod.info.custom_data["gamebanana"]["file_md5"] == hashlib.md5(original).hexdigest()


def test_target_picker_holds_archive_and_cancel_releases_it(handoff, monkeypatch):
    case = handoff
    page = case.page()
    case.profile.category_name = "No matching character"
    selected = []

    def cancel(dialog):
        """在目标对话框仍占用归档时申请清理，再模拟用户取消。"""
        selected.append(True)
        case.dm.delete_archive_when_unused(case.archive)
        assert case.archive.exists(), "目标选择仍未结束时不能删除下载包"
        return dialog.DialogCode.Rejected

    monkeypatch.setattr(GbInstallTargetDialog, "exec", cancel)
    page._after_download(case.file, case.archive, case.profile)
    assert selected and not case.archive.exists()
    assert not case.dm.has_other_consumers(case.file.file_id, set())


def test_missing_profile_releases_the_download_owner(handoff):
    case = handoff
    page = case.page()
    case.dm.retain(case.file.file_id, case.file.md5_checksum, owner=f"download:{case.file.file_id}", path=case.archive)
    case.dm.delete_archive_when_unused(case.archive)
    page._after_download(case.file, case.archive)
    assert not case.archive.exists()
    assert not case.dm.has_other_consumers(case.file.file_id, set())


def test_main_queue_and_real_import_handoff_keep_the_source_until_last_release(handoff, qtbot, monkeypatch):
    case = handoff
    page = case.page()
    host = case.host(page)
    case.dm.retain(case.file.file_id, case.file.md5_checksum, owner="update:first", path=case.archive)
    page._after_download(case.file, case.archive, case.profile)
    assert len(host._pending_gb_installs) == 1
    assert case.dm.has_other_consumers(case.file.file_id, {"update:first", "download:11"})
    case.dm.delete_archive_when_unused(case.archive)
    case.dm.release("update:first")
    assert case.archive.exists(), "主窗口尚未开始的安装必须单独占用源包"
    entered, release = Event(), Event()
    prepare = ModIntakeService.prepare

    def wait_then_prepare(service, request, **kwargs):
        """延后真实文件准备，以检查队列与导入工作线程之间的归档占用。"""
        entered.set()
        assert release.wait(5), "test did not release real import IO"
        return prepare(service, request, **kwargs)

    monkeypatch.setattr(ModIntakeService, "prepare", wait_then_prepare)
    try:
        host._import_active = False
        host._drain_gb_installs()
        assert entered.wait(3)
        assert case.archive.exists() and host._import_active
        case.dm.retain(case.file.file_id, case.file.md5_checksum, owner="update:later", path=case.archive)
    finally:
        release.set()
        qtbot.waitUntil(lambda: not host._import_active, timeout=15000)
    mods = case.manager.get_mods()
    assert len(mods) == 1 and mods[0].info.version == "v1"
    assert Path(mods[0].path).is_file()
    assert case.archive.exists(), "安装成功也不能删掉另一更新仍在读取的包"
    assert not case.dm.has_other_consumers(case.file.file_id, {"update:later"})
    case.dm.release("update:later")
    assert not case.archive.exists()


def test_failed_real_import_releases_ownership_and_preserves_download(handoff, qtbot, monkeypatch):
    case = handoff
    monkeypatch.setattr(case.manager._index, "add_mod", lambda *args, **kwargs: False)
    page = case.page()
    host = case.host(page)
    host._import_active = False
    page._after_download(case.file, case.archive, case.profile)
    qtbot.waitUntil(lambda: not host._import_active, timeout=10000)
    assert not case.manager.get_mods() and case.archive.exists()
    assert not case.dm.has_other_consumers(case.file.file_id, set())
    case.dm.delete_archive_when_unused(case.archive)
    assert not case.archive.exists(), "失败后的安装 owner 必须释放"


def test_deferred_install_keeps_ownership_until_target_is_cancelled(handoff, monkeypatch):
    case = handoff
    page = case.page()
    page._after_download(case.file, case.archive, case.profile, "game-b")
    assert page._pending_installs.pending_game_ids() == {"game-b"}
    case.dm.delete_archive_when_unused(case.archive)
    assert case.archive.exists()
    case.manager.set_game("game-b")
    monkeypatch.setattr(GbInstallTargetDialog, "exec", lambda dialog: dialog.DialogCode.Rejected)
    page._drain_pending_installs("game-b")
    assert not case.archive.exists()
    assert not case.dm.has_other_consumers(case.file.file_id, set())
    assert not page._pending_installs.pending_game_ids()


def test_restart_restores_independent_pending_owners_for_each_game(handoff, monkeypatch):
    case = handoff
    pending = PendingInstallStore()
    for game_id in ("game-a", "game-b"):
        pending.add(PendingInstall.capture(game_id, case.profile, case.file, case.archive))
    original = pending._file.read_bytes()
    assert len(pending.records()) == 2
    assert pending._file.read_bytes() == original, "读取快照不能取走待安装记录"
    page = case.page()
    case.dm.delete_archive_when_unused(case.archive)
    assert case.archive.exists(), "恢复页面时必须先恢复其他游戏的待安装占用"
    page._drain_pending_installs("game-a")
    assert len(case.manager.get_mods()) == 1
    assert case.archive.exists() and pending.pending_game_ids() == {"game-b"}
    case.manager.set_game("game-b")
    monkeypatch.setattr(GbInstallTargetDialog, "exec", lambda dialog: dialog.DialogCode.Rejected)
    page._drain_pending_installs("game-b")
    assert not case.archive.exists()
    assert not case.dm.has_other_consumers(case.file.file_id, set())


def test_discarding_a_missing_pending_archive_releases_ownership(handoff):
    case = handoff
    page = case.page()
    page._after_download(case.file, case.archive, case.profile, "game-b")
    assert case.dm.has_other_consumers(case.file.file_id, set())
    case.archive.unlink()
    case.manager.set_game("game-b")
    page._drain_pending_installs("game-b")
    assert not case.dm.has_other_consumers(case.file.file_id, set())


@pytest.mark.parametrize("owner", ["mod-update:test", "pending-install:game-b:11"])
@pytest.mark.parametrize("include_unowned_installed", [False, True])
def test_settings_purge_defers_owned_archives_and_reports_only_freed_bytes(
    handoff,
    temp_data_dir,
    owner,
    include_unowned_installed,
):
    case = handoff
    protected_bytes = case.archive.read_bytes()
    installed = [(11, case.archive)]
    unowned = None
    freed_bytes = 0
    if include_unowned_installed:
        data = _archive(temp_data_dir / "unowned.zip", b"unowned installed body").read_bytes()
        unowned = case.archive.with_name(f"{hashlib.md5(data).hexdigest()}__12__unowned.zip")
        unowned.write_bytes(data)
        installed.append((12, unowned))
        freed_bytes = len(data)
    data = _archive(temp_data_dir / "uninstalled.zip", b"uninstalled body").read_bytes()
    uninstalled = case.archive.with_name(f"{hashlib.md5(data).hexdigest()}__13__uninstalled.zip")
    uninstalled.write_bytes(data)
    character_id = case.manager.get_characters()[0].id
    for file_id, archive in installed:
        metadata = GbInstallMetadata(
            mod_id=case.profile.mod_id,
            file_id=file_id,
            file_md5=hashlib.md5(archive.read_bytes()).hexdigest(),
            file_ts=2000,
            page_url=case.profile.page_url,
            version="v1",
            submitter_id=1,
            submitter_name="Author",
            last_checked_iso="",
        )
        assert case.manager.import_mod(str(archive), character_id, gb_meta=metadata) is not None
    case.dm.refresh_local_index()
    case.dm.retain(case.file.file_id, case.file.md5_checksum, owner=owner, path=case.archive)
    host = case.host(case.page())
    results = []
    feedback = SimpleNamespace(
        set_download_purge_result=lambda count, freed: results.append((count, freed)),
    )

    host._on_download_purge_requested(feedback)

    assert case.archive.is_file(), "Settings cleanup must preserve archives held by another consumer"
    assert case.archive.read_bytes() == protected_bytes
    assert case.dm.has_local_md5(case.file.md5_checksum) == case.archive
    assert unowned is None or not unowned.exists()
    assert uninstalled.read_bytes() == data
    assert results == [(int(include_unowned_installed), freed_bytes)]
    case.dm.release(owner)
    assert not case.archive.exists(), "The last owner must complete the deferred cleanup"
    assert case.dm.has_local_md5(case.file.md5_checksum) is None
    assert results == [(int(include_unowned_installed), freed_bytes)]


@pytest.mark.parametrize("action", ["resume", "retry"])
def test_flyout_user_action_reclaims_download_before_waking_the_task(manager, qtbot, action):
    dm = manager
    profile = _profile()
    file = profile.files[0]
    task = dm.request(profile, file, game_id="game-a", consumer="mod-update:one")
    if action == "resume":
        dm.pause(file.file_id)
    else:
        dm.cancel(file.file_id)
    assert not dm.has_other_consumers(file.file_id, {"mod-update:one"})
    observed = []
    task.state_changed.connect(lambda state: observed.append(dm.has_other_consumers(file.file_id, {"mod-update:one"})))
    flyout = GbDownloadFlyout(dm)
    qtbot.addWidget(flyout)
    flyout._row_widgets[file.file_id].primary_btn.click()
    assert task.state == "queued"
    assert observed == [True], "用户继续/重试必须在传输唤醒前重新登记下载占用"
    assert not dm.has_other_consumers(file.file_id, {"mod-update:one", f"download:{file.file_id}"})
