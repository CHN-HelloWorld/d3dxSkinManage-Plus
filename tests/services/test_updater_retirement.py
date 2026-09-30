import ctypes
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Native updater retirement is a Windows feature")


def executable(path: Path, marker: bytes = b"") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(Path(sys.executable).read_bytes() + marker)
    return path


def test_cleanup_preserves_current_cache_and_unrecognized_names(tmp_path):
    from src.services.app_update.retirement import cleanup_retired_updaters

    folder = tmp_path / "Updater"
    current = executable(folder / "updater.exe", b"current")
    retired = executable(folder / "updater.exe.old_update.5.1000.1")
    unowned = executable(folder / "updater.exe.old_update")
    os.chmod(retired, stat.S_IREAD)
    extra = executable(folder / "notes.old_update")
    unknown = executable(folder / "updater.exe.old_update.not-owned")
    cached = executable(folder / "Cache/updater.exe.old_update.5.1000.1")
    assert not cleanup_retired_updaters(tmp_path)
    assert not retired.exists()
    assert all(path.exists() for path in (current, unowned, extra, unknown, cached))


def test_cleanup_retains_the_only_recovery_image(tmp_path):
    from src.services.app_update.retirement import cleanup_retired_updaters

    retired = executable(tmp_path / "Updater/updater.exe.old_update.5.1000.1")
    assert cleanup_retired_updaters(tmp_path)
    assert retired.exists()


def test_cleanup_does_not_remove_recovery_when_current_updater_is_invalid(tmp_path):
    from src.services.app_update.retirement import cleanup_retired_updaters

    retired = executable(tmp_path / "Updater/updater.exe.old_update.5.1000.1")
    (retired.parent / "updater.exe").write_bytes(b"broken image")
    assert cleanup_retired_updaters(tmp_path)
    assert retired.exists()


def test_missing_canonical_recovers_the_most_recent_valid_retired_updater(tmp_path):
    from src.services.app_update.retirement import find_or_restore_updater

    folder = tmp_path / "Updater"
    older = executable(folder / "updater.exe.old_update.5.1000.1", b"older")
    newer = executable(folder / "updater.exe.old_update.9.2000.1", b"newer")
    invalid = folder / "updater.exe.old_update.8.3000.1"
    invalid.write_bytes(b"invalid newer-looking file")
    expected = newer.read_bytes()
    result = find_or_restore_updater(tmp_path)
    assert result == folder / "updater.exe"
    assert result.read_bytes() == expected
    assert not newer.exists()
    assert older.exists() and invalid.exists()


def test_existing_canonical_never_gets_replaced_by_a_retired_image(tmp_path):
    from src.services.app_update.retirement import find_or_restore_updater

    current = executable(tmp_path / "Updater/updater.exe", b"current")
    retired = executable(tmp_path / "Updater/updater.exe.old_update.5.1000.1", b"old")
    expected = current.read_bytes()
    assert find_or_restore_updater(tmp_path) == current
    assert current.read_bytes() == expected
    assert retired.exists()


def test_locked_retired_image_is_kept_and_can_be_cleaned_on_retry(tmp_path):
    from src.services.app_update.retirement import cleanup_retired_updaters

    executable(tmp_path / "Updater/updater.exe")
    retired = executable(tmp_path / "Updater/updater.exe.old_update.5.1000.1")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]
    kernel.CreateFileW.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel.CreateFileW(str(retired), 0x80000000, 1, None, 3, 0x80, None)
    assert handle != ctypes.c_void_p(-1).value
    try:
        assert cleanup_retired_updaters(tmp_path)
        assert retired.exists()
    finally:
        kernel.CloseHandle(handle)
    assert not cleanup_retired_updaters(tmp_path)
    assert not retired.exists()


def test_installer_restores_missing_canonical_before_launching(tmp_path):
    from src.services.app_update.installer import AppUpdateInstaller

    old = executable(tmp_path / "Updater/updater.exe.old_update.5.1000.1")
    installer = AppUpdateInstaller(tmp_path / "update.zip", install_dir=tmp_path)
    assert installer.locate_updater() == tmp_path / "Updater/updater.exe"
    assert not old.exists()


def test_startup_cleanup_is_independent_of_update_check_settings(qtbot, temp_data_dir, monkeypatch):
    from PySide6.QtWidgets import QWidget

    from src.core.config import Config
    from src.services.app_update import manager as manager_module

    executable(temp_data_dir / "Updater/updater.exe")
    retired = executable(temp_data_dir / "Updater/updater.exe.old_update.5.1000.1")
    monkeypatch.setattr(manager_module, "is_frozen", lambda: True)
    config = Config()
    config.set("app_update.enabled", False)
    config.set("app_update.on_startup", False)
    window = QWidget()
    qtbot.addWidget(window)
    manager = manager_module.AppUpdateManager(window, config)
    manager._retirement_timer.timeout.emit()
    assert not retired.exists()
    assert not manager._retirement_timer.isActive()


def test_retirement_never_traverses_a_junction_outside_the_installation(tmp_path):
    from src.services.app_update.retirement import cleanup_retired_updaters, find_or_restore_updater

    install = tmp_path / "installed"
    install.mkdir()
    outside = tmp_path / "outside"
    executable(outside / "updater.exe")
    retired = executable(outside / "updater.exe.old_update.5.1000.1")
    result = subprocess.run(
        ["cmd.exe", "/c", "mklink", "/J", str(install / "Updater"), str(outside)],
        capture_output=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    assert result.returncode == 0, result.stderr
    assert not cleanup_retired_updaters(install)
    assert find_or_restore_updater(install) is None
    assert retired.exists()


def test_recovery_does_not_replace_a_directory_at_the_canonical_path(tmp_path):
    from src.services.app_update.retirement import find_or_restore_updater

    old = executable(tmp_path / "Updater/updater.exe.old_update.5.1000.1")
    current = old.parent / "updater.exe"
    current.mkdir()
    (current / "keep.txt").write_text("keep", encoding="utf-8")
    assert find_or_restore_updater(tmp_path) is None
    assert old.exists()
    assert (current / "keep.txt").read_text(encoding="utf-8") == "keep"


def test_truncated_pe_cannot_authorize_deleting_a_usable_recovery_image(tmp_path):
    from src.services.app_update.retirement import cleanup_retired_updaters

    retired = executable(tmp_path / "Updater/updater.exe.old_update.5.1000.1")
    image = Path(sys.executable).read_bytes()
    pe_offset = int.from_bytes(image[60:64], "little")
    (retired.parent / "updater.exe").write_bytes(image[: pe_offset + 26])
    assert cleanup_retired_updaters(tmp_path)
    assert retired.exists()


def test_recovery_skips_a_newer_truncated_pe_image(tmp_path):
    from src.services.app_update.retirement import find_or_restore_updater

    older = executable(tmp_path / "Updater/updater.exe.old_update.5.1000.1", b"usable")
    image = Path(sys.executable).read_bytes()
    pe_offset = int.from_bytes(image[60:64], "little")
    (older.parent / "updater.exe.old_update.5.2000.1").write_bytes(image[: pe_offset + 26])
    expected = older.read_bytes()
    assert find_or_restore_updater(tmp_path).read_bytes() == expected
