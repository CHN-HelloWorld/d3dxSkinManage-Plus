import builtins
import ctypes
import os
import zipfile
from contextlib import contextmanager
from ctypes import wintypes
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.core.mod_package import override_path_for, write_override
from src.core.mod_variant_store import ModVariantStore
from src.services.archive import ArchiveService
from src.utils import fs
from tests.core.test_named_variant_manager import enable
from tests.core.test_variant_preset_switch import _KEY
from tests.core.test_variant_preset_switch import env as env

EXTRA = "namespace = extra\n[Constants]\nglobal persist $extra = 0\n"
FIRST = "namespace = first\n[Constants]\nglobal persist $first = 0\n"


def source(mod, path):
    return SimpleNamespace(
        mod_id=mod.mod_id,
        game_id=mod.game_id,
        path=str(path),
        content_hash="test-source",
        folder_name="Test",
        deployed_path="",
        is_enabled=False,
    )


def file_images(env):
    mm, mod = env["mm"], env["mod"]
    paths = list(Path(mod.path).parent.rglob("*"))
    paths += list(env["migoto"].rglob("*"))
    paths += [mm.variants.prefs_file(), mm.preset_manager._presets_file]
    return {str(p): p.read_bytes() for p in paths if p.is_file()}


@contextmanager
def exclusive_read(path):
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.CreateFileW(str(path), 0x80000000, 0, None, 3, 0x80, None)
    assert handle != ctypes.c_void_p(-1).value, ctypes.get_last_error()
    try:
        yield
    finally:
        assert kernel.CloseHandle(handle)


def complete_variant(env):
    mm, mod, current = enable(env)
    mm.stop_ini_sync()
    mm.variants.stop_watch()
    assert write_override(mod.path, "extra.ini", EXTRA)
    deployed = Path(mm._index.get_mod(mod.mod_id).deployed_path)
    (deployed / "extra.ini").write_text(EXTRA, encoding="utf-8")
    env["user_ini"].write_text(f"[Constants]\n{_KEY} = 2\n$\\extra\\extra = 3\n", encoding="utf-8")
    variant = mm.variants.save_variant(mod.mod_id, "Complete")
    assert {v.variable for v in variant.variables} == {"swapvar", "extra"}
    return mm, mod, current, variant


@pytest.mark.skipif(os.name != "nt", reason="Windows sharing-mode regression")
@pytest.mark.parametrize("command", ["create", "update", "collect", "apply", "default", "preset", "target"])
def test_locked_override_rejects_business_commands_without_publishing(env, command):
    mm, mod, current, variant = complete_variant(env)
    before = file_images(env)
    references = dict(current.mod_variants)
    extra = override_path_for(mod.path, "extra.ini")
    actions = {
        "create": lambda: mm.variants.save_variant(mod.mod_id, "New"),
        "update": lambda: mm.variants.save_variant(mod.mod_id, "Complete", variant_id=variant.variant_id),
        "collect": lambda: mm.variants.collect(trigger="test"),
        "apply": lambda: mm.variants.apply_variant(mod.mod_id, variant.variant_id),
        "default": lambda: mm.variants.apply_variant(mod.mod_id, None),
        "preset": lambda: mm.preset_manager.prepare_preset_apply(current.id),
        "target": lambda: mm.variants._prepare_plan(
            SimpleNamespace(game_id="g1", to_preset_id=current.id, actions=(), operation_id="target-only")
        ),
    }
    with exclusive_read(extra):
        assert extra.stat().st_size == len(EXTRA.encode())
        with pytest.raises(PermissionError):
            extra.read_bytes()
        with pytest.raises((OSError, ValueError), match="extra.ini"):
            actions[command]()
    assert file_images(env) == before
    assert current.mod_variants == references
    assert mm.preset_manager.get_current("g1").id == current.id
    assert not mm.variants._prepared
    assert not mm.preset_manager.has_pending_apply("g1")
    retried = mm.variants.save_variant(mod.mod_id, "Complete", variant_id=variant.variant_id)
    assert {v.variable for v in retried.variables} == {"swapvar", "extra"}


@pytest.mark.parametrize("warm_cache", [False, True])
def test_override_enumeration_failure_does_not_become_a_cached_subset(env, monkeypatch, warm_cache):
    mm, mod, _, _ = complete_variant(env)
    mm.variants._ini_cache.clear()
    if warm_cache:
        mm.variants.build_index()
    base = override_path_for(mod.path, "extra.ini").parent
    original = os.scandir

    def denied(path):
        if Path(fs.normal_path(path)) == base:
            raise PermissionError(f"cannot enumerate {base}")
        return original(path)

    before = file_images(env)
    with monkeypatch.context() as patch:
        patch.setattr(os, "scandir", denied)
        with pytest.raises(OSError, match="enumerate"):
            mm.variants.build_index()
    assert file_images(env) == before
    assert set(mm.variants.build_index().keys_of(mod.mod_id)) == {_KEY, r"$\extra\extra"}


@pytest.mark.parametrize("target", ["override_dir", "marker", "seed_marker"])
def test_override_stat_failure_cannot_hide_existing_content(env, monkeypatch, target):
    mm, mod, _, _ = complete_variant(env)
    base = override_path_for(mod.path, "extra.ini").parent
    path = {"override_dir": base, "marker": Path(mod.path).parent / ".d3dxmod", "seed_marker": base / ".seeded"}[target]
    original = os.stat

    def denied(candidate, *args, **kwargs):
        if Path(fs.normal_path(candidate)) == path:
            raise PermissionError(f"cannot stat {path}")
        return original(candidate, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(os, "stat", denied)
        with pytest.raises(OSError, match="cannot stat"):
            mm.variants.build_index(force=True)
    assert set(mm.variants.build_index().keys_of(mod.mod_id)) == {_KEY, r"$\extra\extra"}


@pytest.mark.parametrize("failure", ["walk", "read", "stat"])
def test_folder_inventory_and_reads_must_be_complete(env, tmp_path, monkeypatch, failure):
    folder = tmp_path / "body"
    (folder / "nested").mkdir(parents=True)
    (folder / "first.ini").write_text(FIRST, encoding="utf-8")
    extra = folder / "nested/extra.ini"
    extra.write_text(EXTRA, encoding="utf-8")
    mod = source(env["mod"], folder)
    mm = env["mm"]
    original = {"walk": os.scandir, "read": builtins.open, "stat": os.stat}[failure]

    def denied(path, *args, **kwargs):
        expected = extra.parent if failure == "walk" else extra
        if Path(fs.normal_path(path)) == expected:
            raise PermissionError(f"unreadable {expected}")
        return original(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        owner, name = {"walk": (os, "scandir"), "read": (builtins, "open"), "stat": (os, "stat")}[failure]
        patch.setattr(owner, name, denied)
        with pytest.raises(OSError, match="unreadable"):
            mm.variants.build_index(mods=[mod])
    index = mm.variants.build_index(mods=[mod])
    assert set(index.keys_of(mod.mod_id)) == {r"$\first\first", r"$\extra\extra"}
    extra.write_text("namespace = changed\n[Constants]\nglobal persist $new = 1\n", encoding="utf-8")
    assert set(mm.variants.build_index(mods=[mod]).keys_of(mod.mod_id)) == {r"$\first\first", r"$\changed\new"}


@pytest.mark.parametrize("failure", ["inventory", "partial", "truncated"])
def test_unseeded_archive_requires_complete_member_evidence(env, tmp_path, monkeypatch, failure):
    path = tmp_path / "body.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("Top/first.ini", FIRST)
        archive.writestr("Top/extra.ini", EXTRA)
    mod = source(env["mod"], path)
    mm = env["mm"]
    with monkeypatch.context() as patch:
        if failure == "inventory":
            patch.setattr(ArchiveService, "list_entries", lambda *args: None)
        else:
            raw = {"Top/first.ini": FIRST.encode()}
            if failure == "truncated":
                raw["Top/extra.ini"] = b"namespace = extra\n"
            patch.setattr(ArchiveService, "read_all_inis", lambda *args: raw)
        with pytest.raises((OSError, ValueError)):
            mm.variants.build_index(mods=[mod])
    assert set(mm.variants.build_index(mods=[mod]).keys_of(mod.mod_id)) == {r"$\first\first", r"$\extra\extra"}


def test_seeded_complete_cache_never_opens_archive_and_retains_all_variables(env, monkeypatch):
    mm, mod, _, _ = complete_variant(env)
    mm.variants._ini_cache.clear()

    def unexpected(*args):
        raise AssertionError("seeded effective read must not open its archive")

    with monkeypatch.context() as patch:
        for name in ("list_entries", "list_contents", "read_all_inis", "read_file"):
            patch.setattr(ArchiveService, name, unexpected)
        for force in (True, False, True):
            assert set(mm.variants.build_index(force=force).keys_of(mod.mod_id)) == {_KEY, r"$\extra\extra"}
    assert {v.variable for v in mm.variants.save_variant(mod.mod_id, "Still complete").variables} == {
        "swapvar",
        "extra",
    }
    assert len(ModVariantStore(mod.path).list()) == 2


@pytest.mark.parametrize("seeded", [False, True])
def test_existing_unreadable_override_never_falls_back_to_original(env, monkeypatch, seeded):
    mm, mod = env["mm"], env["mod"]
    path = override_path_for(mod.path, "merged.ini")
    if not seeded:
        (path.parent / ".seeded").unlink()
    original = builtins.open

    def denied(candidate, *args, **kwargs):
        if Path(fs.normal_path(candidate)) == path:
            raise PermissionError(f"unreadable override {path}")
        return original(candidate, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(builtins, "open", denied)
        with pytest.raises(OSError, match="unreadable override"):
            mm.variants.build_index(mods=[mod])
    assert len(mm.variants.build_index(mods=[mod]).keys_of(mod.mod_id)) == 1


@pytest.mark.parametrize("unchanged_metadata", [False, True])
def test_changed_file_during_complete_read_is_not_cached(env, tmp_path, monkeypatch, unchanged_metadata):
    folder = tmp_path / "body"
    folder.mkdir()
    first, extra = folder / "first.ini", folder / "second.ini"
    first.write_text(FIRST, encoding="utf-8")
    extra.write_text(EXTRA, encoding="utf-8")
    mod = source(env["mod"], folder)
    original = builtins.open
    original_stat, initial_stat = os.stat, first.stat()

    def same_metadata(candidate, *args, **kwargs):
        if Path(fs.normal_path(candidate)) == first:
            return initial_stat
        return original_stat(candidate, *args, **kwargs)

    def change_first(candidate, *args, **kwargs):
        if Path(fs.normal_path(candidate)) == extra:
            first.write_text("namespace = changed\n[Constants]\nglobal persist $new = 1\n", encoding="utf-8")
        return original(candidate, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(builtins, "open", change_first)
        if unchanged_metadata:
            patch.setattr(os, "stat", same_metadata)
        with pytest.raises(OSError, match="发生变化"):
            env["mm"].variants.build_index(mods=[mod])
    assert set(env["mm"].variants.build_index(mods=[mod]).keys_of(mod.mod_id)) == {r"$\changed\new", r"$\extra\extra"}


def test_unchanged_complete_inventory_reuses_previously_confirmed_text(env, monkeypatch):
    mm, mod, _, _ = complete_variant(env)
    path = override_path_for(mod.path, "extra.ini")
    original = builtins.open

    def denied(candidate, *args, **kwargs):
        if Path(fs.normal_path(candidate)) == path:
            raise PermissionError(f"temporarily busy {path}")
        return original(candidate, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(builtins, "open", denied)
        assert set(mm.variants.build_index().keys_of(mod.mod_id)) == {_KEY, r"$\extra\extra"}
        with pytest.raises(OSError, match="temporarily busy"):
            mm.variants.build_index(force=True)
    assert set(mm.variants.build_index().keys_of(mod.mod_id)) == {_KEY, r"$\extra\extra"}


@pytest.mark.parametrize("location", ["override", "deployment"])
def test_round1_unreadable_final_inventory_rejects_without_empty_fallback(env, monkeypatch, location):
    mm, mod, _, variant = complete_variant(env)
    before = env["user_ini"].read_bytes()
    directory = (
        override_path_for(mod.path, "merged.ini").parent
        if location == "override"
        else Path(mm._index.get_mod(mod.mod_id).deployed_path)
    )
    execute = mm.variants._execute
    scan = os.scandir

    def unreadable(path):
        if Path(fs.normal_path(path)) == directory:
            raise PermissionError("final inventory unreadable")
        return scan(path)

    def deny_after_planning(*args, **kwargs):
        with monkeypatch.context() as patch:
            patch.setattr(os, "scandir", unreadable)
            return execute(*args, **kwargs)

    monkeypatch.setattr(mm.variants, "_execute", deny_after_planning)
    with pytest.raises(PermissionError, match="final inventory unreadable"):
        mm.variants.apply_variant(mod.mod_id, variant.variant_id)
    assert env["user_ini"].read_bytes() == before
    assert not list((mm.variants.prefs_file().parent / "variant_operations").glob("*.json"))


def test_round1_folder_source_inventory_addition_invalidates_frozen_index(env, tmp_path):
    from src.core.variant_operation import VariantOperation

    folder = tmp_path / "folder-source"
    folder.mkdir()
    (folder / "first.ini").write_text(FIRST, encoding="utf-8")
    mod = source(env["mod"], folder)
    index = env["mm"].variants.build_index(mods=[mod])
    op = VariantOperation(tmp_path / "operation", "g1", [mod.mod_id], index.source_checks)
    op.prepare()
    added = folder / "extra.ini"
    added.write_text(EXTRA, encoding="utf-8")
    with pytest.raises(RuntimeError, match="INI.*外部修改"):
        op.apply()
    assert added.read_text(encoding="utf-8") == EXTRA
    assert not op.journal_path.exists()


def test_empty_unseeded_archive_is_a_complete_empty_inventory(env, tmp_path):
    path = tmp_path / "empty.zip"
    with zipfile.ZipFile(path, "w"):
        pass
    mod = source(env["mod"], path)
    assert not env["mm"].variants.build_index(mods=[mod]).keys_of(mod.mod_id)
