from __future__ import annotations

import pytest

from src.core import migoto_user_ini as mui
from src.core.mod_runtime_prefs import RuntimePrefsStore
from src.core.mod_variant_service import collect
from src.core.persist_vars import (
    ModIniSource,
    build_attribution_index,
    full_key,
)

_MOD_INI = (
    "; Alpha swap by someone\n"
    "namespace = author\\alpha\\dress\n"
    "\n"
    "[Constants]\n"
    "  global persist $swapvar = 0   ; 循环变体\n"
    "global persist $toggle\n"
    "global $not_persisted = 0\n"
    "\n"
    "[KeySwap]\n"
    "key = VK_A\n"
)


def _snapshot(values: dict[str, str], *, path="D:/3DMigoto/d3dx_user.ini", sha="h"):
    return mui.UserIniSnapshot(
        path=path,
        text="",
        sha256=sha,
        encoding="mbcs",
        values={mui.normalize_var_key(k): v for k, v in values.items()},
    )


# 采集


def _index_one(mod_id="dep-a", *, implicit_prefix=""):
    return build_attribution_index(
        [
            ModIniSource(mod_id, (("merged.ini", _MOD_INI),), implicit_prefix=implicit_prefix),
        ]
    )


_KEY_SWAP = full_key("author\\alpha\\dress", "swapvar")
_KEY_TOGGLE = full_key("author\\alpha\\dress", "toggle")


def test_collect_writes_only_working_state_and_runtime_file():
    store = RuntimePrefsStore()
    report = collect(_snapshot({_KEY_SWAP: "2"}), _index_one(), store)
    assert report.changed
    assert report.attributed == {"dep-a": {_KEY_SWAP: "2"}}
    assert store.snapshot_for("dep-a") == {_KEY_SWAP: "2"}
    assert store.runtime_file.last_seen_hash == "h"
    assert "preset_mod_preferences" not in store.to_dict()


def test_collect_filters_to_enabled_mods():
    store = RuntimePrefsStore()
    index = build_attribution_index(
        [
            ModIniSource("dep-a", (("merged.ini", _MOD_INI),)),
            ModIniSource("dep-b", (("m.ini", "namespace = other\\ns\n[Constants]\nglobal persist $swapvar = 0\n"),)),
        ]
    )
    other = full_key("other\\ns", "swapvar")
    report = collect(_snapshot({_KEY_SWAP: "2", other: "5"}), index, store, enabled_mod_ids=["dep-a"])
    assert report.attributed == {"dep-a": {_KEY_SWAP: "2"}}
    assert "dep-b" not in store.mods


def test_collect_reports_unattributed_and_conflicted():
    shared = "namespace = shared\\ns\n[Constants]\nglobal persist $v = 0\n"
    index = build_attribution_index(
        [
            ModIniSource("dep-a", (("a.ini", shared),)),
            ModIniSource("dep-b", (("b.ini", shared),)),
        ]
    )
    store = RuntimePrefsStore()
    key = full_key("shared\\ns", "v")
    report = collect(_snapshot({key: "1", "$\\ghost\\mod\\var": "9"}), index, store)
    assert report.conflicted == (key,)
    assert report.unattributed == ("$\\ghost\\mod\\var",)
    assert store.mods == {}


def test_missing_values_retain_evidence_but_are_not_confirmed():
    store = RuntimePrefsStore()
    index = _index_one()
    collect(_snapshot({_KEY_SWAP: "2", _KEY_TOGGLE: "1"}), index, store)
    old_source = dict(store.state_for("dep-a").sources[_KEY_TOGGLE])
    collect(_snapshot({_KEY_SWAP: "3"}, sha="h2"), index, store)
    state = store.state_for("dep-a")
    assert state.values == {_KEY_SWAP: "3", _KEY_TOGGLE: "1"}
    assert state.confirmed_keys == (_KEY_SWAP,)
    assert state.sources[_KEY_TOGGLE] == old_source


def test_new_capture_generation_reconfirms_equal_values():
    store = RuntimePrefsStore()
    snapshot = _snapshot({_KEY_SWAP: "2"})
    collect(snapshot, _index_one(), store, capture_generation=1)
    collect(snapshot, _index_one(), store, capture_generation=2)
    assert store.state_for("dep-a").sources[_KEY_SWAP]["capture_generation"] == 2


def test_complete_capture_clears_only_target_runtime_keys_and_retains_evidence(tmp_path):
    path = tmp_path / "d3dx_user.ini"
    path.write_text("[Constants]\n" + f"{_KEY_SWAP} = 2\n{_KEY_TOGGLE} = 1\n$\\other\\v = 9\n", encoding="utf-8")
    snapshot = mui.read_stable(path, settle_seconds=0, attempts=2)
    index, store = _index_one(), RuntimePrefsStore()
    report = collect(snapshot, index, store)
    assert report.attributed == {"dep-a": {_KEY_SWAP: "2", _KEY_TOGGLE: "1"}}
    stripped = mui.remove_keys(snapshot.text, store.snapshot_for("dep-a"))
    assert mui.parse_persisted_values(stripped) == {"$\\other\\v": "9"}
    path.write_bytes(mui.encode_ini_text(stripped, snapshot.encoding))
    collect(mui.read_stable(path, settle_seconds=0, attempts=2), index, store)
    assert store.state_for("dep-a").values == {_KEY_SWAP: "2", _KEY_TOGGLE: "1"}
    assert store.state_for("dep-a").confirmed_keys == ()


@pytest.mark.parametrize("stored", ["2", "2.0", "2.00000000"])
def test_float_semantics_survive_runtime_roundtrip(tmp_path, stored):
    from src.core.variant_operation import atomic_write

    index, store = _index_one(), RuntimePrefsStore()
    report = collect(_snapshot({_KEY_SWAP: stored, _KEY_TOGGLE: "0"}), index, store)
    assert report.attributed == {"dep-a": {_KEY_SWAP: stored, _KEY_TOGGLE: "0"}}

    original = f"[Constants]\n{_KEY_SWAP} = 2\n{_KEY_TOGGLE} = 0\n$\\other\\v = 9\n"
    output = mui.apply_values(original, store.snapshot_for("dep-a"))
    assert output == original
    path = tmp_path / "d3dx_user.ini"
    atomic_write(path, mui.encode_ini_text(output, "utf-8"))
    snapshot = mui.read_stable(path, settle_seconds=0, attempts=2)
    assert snapshot is not None
    assert snapshot.values == {_KEY_SWAP: "2", _KEY_TOGGLE: "0", "$\\other\\v": "9"}
    assert mui.values_equal(snapshot.values[_KEY_SWAP], stored)
