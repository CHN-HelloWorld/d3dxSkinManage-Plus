import json
from pathlib import Path

import pytest

from src.core import mod_runtime_prefs as prefs
from src.core.migoto_user_ini import UserIniSnapshot
from src.core.mod_variant_service import collect
from src.core.persist_vars import ModIniSource, build_attribution_index

KEY_A = r"$\skin\a"
KEY_B = r"$\skin\b"


def index():
    return build_attribution_index(
        [
            ModIniSource(
                "mod-a",
                (("main.ini", "namespace = skin\n[Constants]\nglobal persist $a = 0\nglobal persist $b = 1\n"),),
            )
        ]
    )


def capture(store, values, *, generation="deployment-1", sequence=1, enabled=("mod-a",)):
    snapshot = UserIniSnapshot(Path("user.ini"), "", f"hash-{sequence}", "utf-8", values)
    return collect(
        snapshot, index(), store, enabled_mod_ids=enabled, deployment_generation=generation, capture_generation=sequence
    )


def test_preferences_have_only_mod_scoped_working_state():
    raw = prefs.RuntimePrefsStore().to_dict()
    assert raw.get("mods") == {}
    assert "mod_recent" not in raw
    assert "preset_mod_preferences" not in raw


def test_capture_updates_working_values_without_selecting_variant():
    store = prefs.RuntimePrefsStore()
    capture(store, {KEY_A: "2", KEY_B: "3"})
    state = store.state_for("mod-a")
    assert state.values == {KEY_A: "2", KEY_B: "3"}
    assert state.selected_variant_id is None
    assert set(state.confirmed_keys) == {KEY_A, KEY_B}
    assert state.declaration_fingerprint
    assert "preset_mod_preferences" not in store.to_dict()


def test_missing_key_retains_old_value_and_source_but_is_not_confirmed():
    store = prefs.RuntimePrefsStore()
    capture(store, {KEY_A: "2", KEY_B: "3"})
    previous_source = dict(store.state_for("mod-a").sources[KEY_B])
    capture(store, {KEY_A: "4"}, sequence=2)
    state = store.state_for("mod-a")
    assert state.values == {KEY_A: "4", KEY_B: "3"}
    assert set(state.confirmed_keys) == {KEY_A}
    assert state.sources[KEY_B] == previous_source
    assert state.capture_generation == 2


def test_disabled_mod_does_not_capture_or_create_a_state():
    store = prefs.RuntimePrefsStore()
    report = capture(store, {KEY_A: "9"}, enabled=())
    assert report.attributed == {}
    assert store.to_dict()["mods"] == {}


def test_later_deployment_cannot_confirm_previous_partial_values():
    store = prefs.RuntimePrefsStore()
    capture(store, {KEY_A: "2"})
    capture(store, {KEY_B: "3"}, generation="deployment-2", sequence=2)
    state = store.state_for("mod-a")
    assert set(state.confirmed_keys) == {KEY_B}
    assert state.sources[KEY_A]["deployment_generation"] == "deployment-1"
    assert state.sources[KEY_B]["deployment_generation"] == "deployment-2"


def test_stale_capture_cannot_overwrite_latest_state():
    store = prefs.RuntimePrefsStore()
    capture(store, {KEY_A: "5", KEY_B: "6"}, sequence=3)
    before = store.to_dict()
    report = capture(store, {KEY_A: "1", KEY_B: "1"}, sequence=2)
    assert not report.changed
    assert store.to_dict() == before


def test_selected_variant_survives_capture_and_roundtrip(tmp_path):
    store = prefs.RuntimePrefsStore()
    store.state_for("mod-a").selected_variant_id = "a" * 32
    capture(store, {KEY_A: "5", KEY_B: "6"})
    path = tmp_path / "mod_preferences.json"
    assert prefs.save(path, store)
    restored = prefs.load(path).state_for("mod-a")
    assert restored.selected_variant_id == "a" * 32
    assert restored.values == {KEY_A: "5", KEY_B: "6"}
    assert restored.sources[KEY_A]["source"] == "game"


def test_stale_loaded_preferences_cannot_replace_a_newer_write(tmp_path):
    path = tmp_path / "mod_preferences.json"
    initial = prefs.RuntimePrefsStore()
    initial.state_for("mod-a").values = {KEY_A: "1"}
    assert prefs.save(path, initial)
    stale = prefs.load(path)
    newer = prefs.load(path)
    newer.state_for("mod-b").values = {KEY_B: "7"}
    assert prefs.save(path, newer)
    before = path.read_bytes()
    stale.state_for("mod-a").values = {KEY_A: "2"}

    assert not prefs.save(path, stale)
    assert path.read_bytes() == before
    assert prefs.load(path).snapshot_for("mod-b") == {KEY_B: "7"}


def test_backup_read_cannot_replace_an_externally_repaired_primary(tmp_path):
    path = tmp_path / "mod_preferences.json"
    original = prefs.RuntimePrefsStore()
    original.state_for("mod-a").values = {KEY_A: "1"}
    assert prefs.save(path, original)
    original.state_for("mod-a").values = {KEY_A: "2"}
    assert prefs.save(path, original)
    path.write_bytes(b"damaged")
    recovered = prefs.load(path)
    assert recovered.snapshot_for("mod-a") == {KEY_A: "1"}
    repaired = original.to_dict()
    repaired["mods"]["mod-b"] = {"values": {KEY_B: "7"}}
    path.write_text(json.dumps(repaired), encoding="utf-8")
    before = path.read_bytes()

    assert not prefs.save(path, recovered)
    assert path.read_bytes() == before


def test_stable_read_rechecks_after_reading_bytes(tmp_path, monkeypatch):
    from src.core import migoto_user_ini as user_ini

    path = tmp_path / "user.ini"
    path.write_text("[Constants]\n$v = 1\n", encoding="utf-8")
    original = user_ini.fs.read_bytes

    def changing_file(target):
        raw = original(target)
        path.write_text("[Constants]\n$v = 999\n", encoding="utf-8")
        return raw

    monkeypatch.setattr(user_ini.fs, "read_bytes", changing_file)
    with pytest.raises(user_ini.UserIniUnstableError):
        user_ini.read_stable(path, settle_seconds=0)
