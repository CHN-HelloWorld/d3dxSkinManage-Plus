import json

import pytest


def operation(tmp_path, changes):
    from src.core.variant_operation import FileChange, VariantOperation

    return VariantOperation(
        tmp_path / "variant_operations",
        "game",
        ("mod",),
        [FileChange(path, before, after) for path, before, after in changes],
    )


def test_journal_exists_before_first_configuration_write(tmp_path, monkeypatch):
    from src.core import variant_operation as module

    target = tmp_path / "state.ini"
    target.write_bytes(b"old")
    op = operation(tmp_path, [(target, b"old", b"new")])
    real = module.atomic_write

    def observe(path, payload):
        if path == target:
            data = json.loads(op.journal_path.read_text(encoding="utf-8"))
            assert data["changes"][0]["before_hash"]
            assert data["phase"] == "prepared"
        return real(path, payload)

    monkeypatch.setattr(module, "atomic_write", observe)
    op.apply()
    assert target.read_bytes() == b"new"
    assert op.journal_path.is_file()
    op.commit()
    assert not op.journal_path.exists()


def test_write_failure_rolls_back_prior_changes(tmp_path, monkeypatch):
    from src.core import variant_operation as module

    first, second = tmp_path / "first.ini", tmp_path / "second.ini"
    first.write_bytes(b"a")
    second.write_bytes(b"b")
    op = operation(tmp_path, [(first, b"a", b"A"), (second, b"b", b"B")])
    real = module.atomic_write

    def fail(path, payload):
        if path == second and payload == b"B":
            raise OSError("disk full")
        return real(path, payload)

    monkeypatch.setattr(module, "atomic_write", fail)
    with pytest.raises(OSError, match="disk full"):
        op.apply()
    assert first.read_bytes() == b"a"
    assert second.read_bytes() == b"b"


def test_external_edit_is_preserved_and_requires_recovery(tmp_path):
    target = tmp_path / "state.ini"
    target.write_bytes(b"old")
    op = operation(tmp_path, [(target, b"old", b"new")])
    op.apply()
    target.write_bytes(b"external")
    problems = op.rollback()
    assert problems
    assert target.read_bytes() == b"external"
    assert json.loads(op.journal_path.read_text(encoding="utf-8"))["phase"] == "recovery_required"


def test_crash_between_files_is_recovered_from_durable_record(tmp_path, monkeypatch):
    from src.core import variant_operation as module

    first, second = tmp_path / "first.ini", tmp_path / "second.ini"
    first.write_bytes(b"a")
    second.write_bytes(b"b")
    op = operation(tmp_path, [(first, b"a", b"A"), (second, b"b", b"B")])
    real = module.atomic_write

    def interrupt(path, payload):
        if path == second:
            raise SystemExit("simulated process stop")
        return real(path, payload)

    monkeypatch.setattr(module, "atomic_write", interrupt)
    with pytest.raises(SystemExit):
        op.apply()
    assert first.read_bytes() == b"A"
    monkeypatch.setattr(module, "atomic_write", real)
    restored = module.VariantOperation.load(op.journal_path, allowed_roots=[tmp_path])
    assert restored.rollback() == ()
    assert first.read_bytes() == b"a"
    assert second.read_bytes() == b"b"


def test_failed_delete_restores_file_and_references(tmp_path, monkeypatch):
    from src.core import variant_operation as module

    variant, presets = tmp_path / "variant.json", tmp_path / "presets.json"
    variant.write_bytes(b"variant")
    presets.write_bytes(b"referenced")
    op = operation(tmp_path, [(variant, b"variant", None), (presets, b"referenced", b"default")])
    real = module.atomic_write

    def fail(path, payload):
        if path == presets and payload == b"default":
            raise OSError("read only")
        return real(path, payload)

    monkeypatch.setattr(module, "atomic_write", fail)
    with pytest.raises(OSError):
        op.apply()
    assert variant.read_bytes() == b"variant"
    assert presets.read_bytes() == b"referenced"


def test_recovery_rejects_paths_outside_authorized_roots(tmp_path):
    from src.core.variant_operation import VariantOperation

    safe = tmp_path / "safe"
    safe.mkdir()
    target = tmp_path / "outside.ini"
    op = operation(tmp_path, [(target, None, b"bad")])
    op.prepare()
    with pytest.raises(ValueError, match="路径"):
        VariantOperation.load(op.journal_path, allowed_roots=[safe])


def test_failed_commit_record_still_allows_rollback(tmp_path, monkeypatch):
    from src.core import variant_operation as module

    target = tmp_path / "state.ini"
    target.write_bytes(b"old")
    op = operation(tmp_path, [(target, b"old", b"new")])
    op.apply()
    real = module.atomic_write

    def fail(path, payload):
        if path == op.journal_path and json.loads(payload)["phase"] == "committed":
            raise OSError("commit journal unavailable")
        return real(path, payload)

    monkeypatch.setattr(module, "atomic_write", fail)
    with pytest.raises(OSError, match="commit journal"):
        op.commit()
    assert op.rollback() == ()
    assert target.read_bytes() == b"old"
    assert not op.journal_path.exists()


def test_unchanged_files_still_participate_in_cas(tmp_path, monkeypatch):
    target = tmp_path / "same.ini"
    target.write_bytes(b"same")
    op = operation(tmp_path, [(target, b"same", b"same")])
    op.prepare()
    target.write_bytes(b"external")
    with pytest.raises(RuntimeError, match="外部修改"):
        op.apply()
    assert target.read_bytes() == b"external"


def test_unchanged_file_is_checked_without_replacement(tmp_path, monkeypatch):
    from src.core import variant_operation as module

    target = tmp_path / "same.ini"
    target.write_bytes(b"same")
    before = target.stat()
    op = operation(tmp_path, [(target, b"same", b"same")])
    original = module.atomic_write

    def observe(path, payload):
        assert path != target, "an unchanged file only needs a readback check"
        return original(path, payload)

    monkeypatch.setattr(module, "atomic_write", observe)
    op.apply()
    op.commit()
    assert target.stat().st_mtime_ns == before.st_mtime_ns


@pytest.mark.parametrize("phase", ["prepared", "applied", "reloaded"])
def test_round1_read_only_guard_changes_do_not_require_restoration(tmp_path, phase):
    from src.core.variant_operation import VariantOperation

    author, runtime = tmp_path / "author.ini", tmp_path / "user.ini"
    author.write_bytes(b"author")
    runtime.write_bytes(b"old")
    op = operation(tmp_path, [(runtime, b"old", b"new"), (author, b"author", b"author")])
    if phase == "prepared":
        op.prepare()
        author.write_bytes(b"external")
        with pytest.raises(RuntimeError, match="外部修改"):
            op.apply()
    else:
        op.apply()
        author.write_bytes(b"external")
        if phase == "reloaded":
            op = VariantOperation.load(op.journal_path, allowed_roots=[tmp_path])
        assert op.rollback() == ()
    assert op.rollback() == ()
    assert author.read_bytes() == b"external"
    assert runtime.read_bytes() == b"old"
    assert not op.journal_path.exists()
    retry = operation(tmp_path, [(author, b"external", b"external"), (runtime, b"old", b"retry")])
    retry.apply()
    retry.commit()
    assert runtime.read_bytes() == b"retry"


def test_round1_inventory_journal_survives_reload_and_only_rolls_back_writes(tmp_path):
    from src.core.variant_operation import FileChange, IniInventoryGuard, VariantOperation

    source = tmp_path / "source"
    source.mkdir()
    (source / "author.ini").write_bytes(b"original")
    runtime = tmp_path / "user.ini"
    runtime.write_bytes(b"old")
    op = VariantOperation(
        tmp_path / "operations",
        "g",
        ["m"],
        [IniInventoryGuard.deployed(source, ["author.ini"]), FileChange(runtime, b"old", b"new")],
    )
    op.apply()
    loaded = VariantOperation.load(op.journal_path, allowed_roots=[tmp_path])
    added = source / "external.ini"
    added.write_bytes(b"external")
    with pytest.raises(RuntimeError, match="INI.*外部修改"):
        loaded.verify_applied()
    assert loaded.rollback() == ()
    assert runtime.read_bytes() == b"old"
    assert added.read_bytes() == b"external"
    assert not loaded.journal_path.exists()
