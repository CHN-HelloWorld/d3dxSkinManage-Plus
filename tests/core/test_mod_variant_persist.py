from __future__ import annotations

import json

import pytest

from src.core import migoto_user_ini as mui
from src.core import mod_runtime_prefs as prefs
from src.core.persist_vars import (
    ModIniSource,
    build_attribution_index,
    full_key,
    implicit_namespace,
    parse_namespace,
    parse_persist_vars,
)

# 上游写出的真实形态：6 行注释头 + 唯一一个 [Constants] 段
_USER_INI = (
    "; AUTOMATICALLY GENERATED FILE - DO NOT EDIT\n"
    ";\n"
    "; 3DMigoto will overwrite this file whenever any persistent settings are\n"
    "; altered by hot key or command list.\n"
    ";\n"
    "[Constants]\n"
    "$\\mods\\alpha\\merged.ini\\swapvar = 2\n"
    "$\\exampleauthor\\beta\\dress\\swapvar = 1\n"
)


# 编码 / 规范化 / 比较


def test_ansi_roundtrip_is_byte_exact():
    raw = "[Constants]\n$\\mods\\角色\\swapvar = 2\n".encode("mbcs")
    text, encoding = mui.decode_ini_bytes(raw)
    assert encoding == "mbcs"
    assert mui.encode_ini_text(text, encoding) == raw


def test_utf8_without_bom_is_detected_and_roundtrips():
    raw = "[Constants]\n$\\mods\\页\\merged.ini\\swapvar = 1\n".encode()
    text, encoding = mui.decode_ini_bytes(raw)
    assert encoding == "utf-8"
    assert "$\\mods\\页\\merged.ini\\swapvar" in text
    assert mui.encode_ini_text(text, encoding) == raw


def test_ascii_only_file_roundtrips_whichever_encoding_is_guessed():
    raw = _USER_INI.encode("ascii")
    text, encoding = mui.decode_ini_bytes(raw)
    assert mui.encode_ini_text(text, encoding) == raw


def test_utf8_bom_is_respected():
    raw = "[Constants]\n$\\a\\v = 1\n".encode("utf-8-sig")
    text, encoding = mui.decode_ini_bytes(raw)
    assert encoding == "utf-8-sig"
    assert "$\\a\\v" in text


def test_decode_passthrough_repairs_ansi_bytes_read_as_utf8():
    smuggled = "角色".encode("mbcs").decode("utf-8", errors="surrogateescape")
    assert smuggled != "角色"
    assert mui.decode_passthrough_text(smuggled) == "角色"
    # 正常文本零改动
    assert mui.decode_passthrough_text("mods\\页") == "mods\\页"
    assert mui.decode_passthrough_text("") == ""


def test_key_variants_bridge_unresolvable_encoding_ambiguity():
    """「页」的 GBK 字节 D2 B3 同时是合法 UTF-8，可解码为 ҳ。

    键匹配需将同一组字节的两种解码结果视为等价，以识别本地代码页写出的中文命名空间。
    """
    ansi_read_as_utf8 = "页".encode("mbcs").decode("utf-8")
    assert ansi_read_as_utf8 == "ҳ"
    variants = mui.key_variants(f"$\\mods\\{ansi_read_as_utf8}\\merged.ini\\SwapVar")
    assert mui.normalize_var_key("$\\mods\\页\\merged.ini\\swapvar") in variants


def test_key_variants_short_circuit_on_ascii():
    assert mui.key_variants("$\\Mods\\Alpha\\swapvar") == ("$\\mods\\alpha\\swapvar",)


def test_normalize_var_key_unifies_both_sides_encodings():
    from_user_ini = "$\\mods\\角色\\merged.ini\\SwapVar"
    from_ansi_ini = (
        "$\\mods\\" + "角色".encode("mbcs").decode("utf-8", errors="surrogateescape") + "\\merged.ini\\SwapVar"
    )
    assert mui.normalize_var_key(from_ansi_ini) == mui.normalize_var_key(from_user_ini)


def test_remove_keys_matches_across_encoding_interpretations():
    text = "[Constants]\n$\\mods\\ҳ\\merged.ini\\swapvar = 1\n$\\keep\\me = 3\n"
    out = mui.remove_keys(text, ["$\\mods\\页\\merged.ini\\swapvar"])
    assert "swapvar" not in out
    assert "$\\keep\\me = 3" in out


def test_normalize_var_key_lowers_and_unifies_separator():
    assert mui.normalize_var_key("  $/Mods/Alpha/SwapVar ") == "$\\mods\\alpha\\swapvar"


@pytest.mark.parametrize(
    ("a", "b", "same"),
    [
        ("2", "2.0", True),
        ("2", "2.00000000", True),
        ("2", "3", False),
        ("0.5", "0.500000001", True),  # %.9g 精度内
        ("1", "1.01", False),
        ("abc", "ABC", True),  # 非数值回退大小写不敏感串比
        ("abc", "abd", False),
    ],
)
def test_values_equal_uses_float_semantics(a, b, same):
    assert mui.values_equal(a, b) is same


# 定位


def test_resolve_user_config_defaults_to_d3dx_user_ini(tmp_path):
    (tmp_path / "d3dx.ini").write_text("[Include]\n", encoding="utf-8")
    assert mui.resolve_user_config_path(tmp_path) == tmp_path / "d3dx_user.ini"


def test_resolve_user_config_honours_include_declaration(tmp_path):
    (tmp_path / "d3dx.ini").write_text(
        "[Logging]\nuser_config = ignored.ini\n[Include]\nuser_config = sub\\my_user.ini  ; 注释\n",
        encoding="utf-8",
    )
    assert mui.resolve_user_config_path(tmp_path) == tmp_path / "sub" / "my_user.ini"


def test_resolve_user_config_returns_none_without_migoto_path(tmp_path):
    assert mui.resolve_user_config_path("") is None
    assert mui.resolve_user_config_path(None) is None
    assert mui.resolve_user_config_path(tmp_path / "missing") is None


# 稳定读取


def test_read_stable_parses_values(tmp_path):
    p = tmp_path / "d3dx_user.ini"
    p.write_bytes(_USER_INI.encode("mbcs"))
    snap = mui.read_stable(p, settle_seconds=0, attempts=2)
    assert snap is not None
    assert snap.values == {
        "$\\mods\\alpha\\merged.ini\\swapvar": "2",
        "$\\exampleauthor\\beta\\dress\\swapvar": "1",
    }
    assert snap.sha256 == mui.sha256_of(snap.text, snap.encoding)


def test_read_stable_missing_file_returns_none(tmp_path):
    assert mui.read_stable(tmp_path / "nope.ini") is None


def test_read_stable_raises_when_file_keeps_changing(tmp_path):
    p = tmp_path / "d3dx_user.ini"
    p.write_text("[Constants]\n$\\a\\v = 0\n", encoding="utf-8")
    grow = {"n": 0}

    def _mutate(_seconds):
        grow["n"] += 1
        p.write_text("[Constants]\n$\\a\\v = 0\n" + "; pad\n" * grow["n"], encoding="utf-8")

    with pytest.raises(mui.UserIniUnstableError):
        mui.read_stable(p, settle_seconds=0, attempts=3, sleep=_mutate)


def test_read_stable_ignores_conflicting_duplicate_key(tmp_path):
    p = tmp_path / "d3dx_user.ini"
    p.write_text("[Constants]\n$\\a\\v = 1\n$\\a\\v = 7\n", encoding="utf-8")
    snap = mui.read_stable(p, settle_seconds=0, attempts=2)
    assert snap is not None and snap.values == {}


# 局部改写


def test_apply_values_replaces_only_value_and_keeps_indent_and_comment():
    text = "[Constants]\n  $\\a\\v = 1   ; 用户注释\n"
    out = mui.apply_values(text, {"$\\A\\V": "5"})
    assert out == "[Constants]\n  $\\a\\v = 5   ; 用户注释\n"


def test_apply_values_preserves_crlf():
    text = "[Constants]\r\n$\\a\\v = 1\r\n"
    out = mui.apply_values(text, {"$\\a\\v": "9"})
    assert out == "[Constants]\r\n$\\a\\v = 9\r\n"


def test_apply_values_skips_write_when_value_is_numerically_equal():
    text = "[Constants]\n$\\a\\v = 2\n"
    assert mui.apply_values(text, {"$\\a\\v": "2.0"}) == text


def test_apply_values_appends_missing_key_into_constants():
    out = mui.apply_values(_USER_INI, {"$\\new\\ns\\var": "3"})
    assert "$\\new\\ns\\var = 3" in out
    # 仍然只有一个 [Constants] 段（上游只会写这一个）
    assert out.count("[Constants]") == 1
    assert mui.parse_persisted_values(out)["$\\new\\ns\\var"] == "3"


def test_apply_values_creates_constants_section_when_absent():
    out = mui.apply_values("; empty file\n", {"$\\a\\v": "1"})
    assert "[Constants]" in out
    assert mui.parse_persisted_values(out) == {"$\\a\\v": "1"}


def test_remove_keys_drops_whole_line():
    out = mui.remove_keys(_USER_INI, ["$\\MODS\\Alpha\\merged.ini\\swapvar"])
    assert "alpha" not in out
    assert mui.parse_persisted_values(out) == {"$\\exampleauthor\\beta\\dress\\swapvar": "1"}
    assert out.count("[Constants]") == 1


def test_runtime_operation_atomic_write_roundtrip(tmp_path):
    from src.core.variant_operation import atomic_write

    p = tmp_path / "d3dx_user.ini"
    payload = mui.encode_ini_text(_USER_INI, "mbcs")
    atomic_write(p, payload)
    assert p.read_bytes() == payload
    assert list(tmp_path.iterdir()) == [p]
    snap = mui.read_stable(p, settle_seconds=0, attempts=2)
    assert snap is not None and len(snap.values) == 2


# persist 声明解析


def test_parse_namespace_allows_leading_comments():
    text = "; by someone\n\nnamespace = ExampleAuthor\\Beta\\Dress\n[Constants]\n"
    assert parse_namespace(text) == "ExampleAuthor\\Beta\\Dress"


def test_parse_namespace_stops_at_first_section():
    assert parse_namespace("[Constants]\nnamespace = too\\late\n") == ""


def test_parse_persist_vars_requires_both_global_and_persist():
    text = (
        "[Constants]\n"
        "global persist $swapvar = 0\n"
        "persist global $other = 1\n"
        "global $not_persisted = 0\n"
        "local $scratch = 0\n"
        "[KeySwap]\n"
        "global persist $outside_constants = 0\n"
    )
    assert parse_persist_vars(text) == ["swapvar", "other"]


def test_implicit_namespace_includes_ini_filename():
    assert implicit_namespace("Mods\\Alpha-Skin", "merged.ini") == ("mods\\alpha-skin\\merged.ini")
    assert implicit_namespace("Mods", "sub/deep.ini") == "mods\\sub\\deep.ini"
    assert implicit_namespace("", "merged.ini") == "merged.ini"


# 归属索引

_DECL = "[Constants]\nglobal persist $swapvar = 0\n"


def test_explicit_namespace_attribution_is_path_independent():
    src = ModIniSource("dep-a", (("merged.ini", "namespace = my\\ns\n" + _DECL),))
    index = build_attribution_index([src])
    assert index.resolve("$\\MY\\NS\\SWAPVAR") == "dep-a"
    assert not index.conflicts


def test_implicit_namespace_attribution_uses_deploy_prefix():
    src = ModIniSource("dep-a", (("merged.ini", _DECL),), implicit_prefix="Mods\\Alpha")
    index = build_attribution_index([src])
    assert index.resolve("$\\mods\\alpha\\merged.ini\\swapvar") == "dep-a"


def test_missing_namespace_is_skipped_with_diagnostic():
    src = ModIniSource("dep-a", (("merged.ini", _DECL),), implicit_prefix="")
    index = build_attribution_index([src])
    assert index.owner == {}
    assert index.resolve("$\\merged.ini\\swapvar") is None
    assert any("未声明 namespace" in d for d in index.diagnostics)


def test_empty_mod_id_is_skipped():
    index = build_attribution_index(
        [
            ModIniSource("", (("merged.ini", "namespace = x\n" + _DECL),)),
        ]
    )
    assert index.owner == {}
    assert any("mod_id 为空" in d for d in index.diagnostics)


def test_same_namespace_and_var_in_two_mods_is_conflict():
    shared = "namespace = shared\\ns\n" + _DECL
    index = build_attribution_index(
        [
            ModIniSource("dep-a", (("merged.ini", shared),)),
            ModIniSource("dep-b", (("other.ini", shared),)),
        ]
    )
    key = full_key("shared\\ns", "swapvar")
    assert index.resolve(key) is None
    assert index.conflicts[key] == ("dep-a", "dep-b")
    assert any("冲突" in d for d in index.diagnostics)
    # 冲突键仍登记在两侧的 keys_of（供「删 d3dx_user 键」定位）
    assert key in index.keys_of("dep-a") and key in index.keys_of("dep-b")


def test_same_mod_declaring_key_twice_is_an_ownership_conflict():
    src = ModIniSource(
        "dep-a",
        (
            ("merged.ini", "namespace = ns\n" + _DECL),
            ("extra.ini", "namespace = ns\n" + _DECL),
        ),
    )
    index = build_attribution_index([src])
    key = full_key("ns", "swapvar")
    assert index.resolve(key) is None
    assert key in index.conflicts


def test_index_matches_real_user_ini_keys():
    index = build_attribution_index(
        [
            ModIniSource("dep-alpha", (("merged.ini", _DECL),), implicit_prefix="mods\\alpha"),
            ModIniSource("dep-beta", (("merged.ini", "namespace = ExampleAuthor\\Beta\\Dress\n" + _DECL),)),
        ]
    )
    parsed = mui.parse_persisted_values(_USER_INI)
    assert {k: index.resolve(k) for k in parsed} == {
        "$\\mods\\alpha\\merged.ini\\swapvar": "dep-alpha",
        "$\\exampleauthor\\beta\\dress\\swapvar": "dep-beta",
    }


# 偏好存储


def test_prefs_path_requires_resource_dir(tmp_path):
    assert prefs.prefs_path("", tmp_path) is None
    assert prefs.prefs_path("Example Game A", tmp_path) == (tmp_path / "Example Game A" / "mod_preferences.json")


def test_prefs_path_is_outside_mods_dir(tmp_path):
    p = prefs.prefs_path("Game", tmp_path)
    assert p is not None and "mods" not in p.relative_to(tmp_path).parts


def test_prefs_roundtrip(tmp_path):
    p = tmp_path / prefs.PREFS_FILENAME
    store = prefs.RuntimePrefsStore()
    store.runtime_file = prefs.RuntimeFileInfo(path="D:/3DMigoto/d3dx_user.ini", last_seen_hash="abc")
    assert store.update_recent("dep-a", {"$\\NS\\Var": "2"})
    store.state_for("dep-a").selected_variant_id = "variant-a"
    assert prefs.save(p, store)

    again = prefs.load(p)
    assert again.runtime_file.last_seen_hash == "abc"
    # 键在写入时已规范化为小写
    assert again.snapshot_for("dep-a") == {"$\\ns\\var": "2"}
    assert again.state_for("dep-a").selected_variant_id == "variant-a"
    assert again.state_for("dep-a").confirmed_keys == ()
    assert json.loads(p.read_text(encoding="utf-8"))["schema"] == prefs.SCHEMA


def test_working_snapshot_is_independent_and_returns_a_copy():
    store = prefs.RuntimePrefsStore()
    store.update_recent("dep-a", {"$\\ns\\var": "1"})
    returned = store.snapshot_for("dep-a")
    returned["$\\ns\\var"] = "3"
    assert store.snapshot_for("dep-a") == {"$\\ns\\var": "1"}
    assert store.snapshot_for("dep-unknown") == {}
    assert "preset_mod_preferences" not in store.to_dict()


def test_update_recent_does_not_clear_keys_absent_this_round():
    store = prefs.RuntimePrefsStore()
    store.update_recent("dep-a", {"$\\ns\\a": "1", "$\\ns\\b": "2"})
    store.update_recent("dep-a", {"$\\ns\\a": "5"})
    assert store.snapshot_for("dep-a") == {"$\\ns\\a": "5", "$\\ns\\b": "2"}
    assert store.state_for("dep-a").confirmed_keys == ()


def test_working_state_roundtrip_keeps_capture_evidence():
    store = prefs.RuntimePrefsStore()
    store.update_recent("dep-a", {"$\\ns\\v": "1"})
    state = store.state_for("dep-a")
    state.confirmed_keys = ("$\\ns\\v",)
    state.declaration_fingerprint = "declarations"
    state.deployment_generation = "deployment-1"
    state.capture_generation = 2
    state.captured_at = "2026-10-19T00:00:00"
    state.sources = {"$\\ns\\v": {"path": "d3dx_user.ini", "hash": "abc"}}
    again = prefs.RuntimePrefsStore.from_dict(store.to_dict())
    assert again.state_for("dep-a") == state
    again.state_for("dep-a").sources["$\\ns\\v"]["hash"] = "other"
    assert state.sources["$\\ns\\v"]["hash"] == "abc"


def test_authoritative_mod_deletion_clears_only_its_private_state():
    store = prefs.RuntimePrefsStore()
    store.update_recent("dep-a", {"$\\ns\\v": "1"})
    store.state_for("dep-a").selected_variant_id = "variant-a"
    store.update_recent("dep-b", {"$\\ns\\w": "3"})
    assert store.drop_deployment("dep-a")
    assert "dep-a" not in store.mods
    assert store.snapshot_for("dep-b") == {"$\\ns\\w": "3"}


def test_unknown_top_level_keys_are_preserved():
    store = prefs.RuntimePrefsStore.from_dict({"schema": 1, "future_field": {"a": 1}, "mod_recent": {}})
    assert store.to_dict()["future_field"] == {"a": 1}


def test_malformed_entries_are_rejected_instead_of_losing_evidence():
    with pytest.raises(ValueError):
        prefs.RuntimePrefsStore.from_dict(
            {
                "runtime_file": "not-a-dict",
                "mods": {"dep-a": "junk"},
            }
        )


def test_load_falls_back_to_bak_and_preserves_broken_file(tmp_path):
    p = tmp_path / prefs.PREFS_FILENAME
    good = prefs.RuntimePrefsStore()
    good.update_recent("dep-a", {"$\\ns\\v": "7"})
    assert prefs.save(p, good)
    # 再存一次以产生 .bak（内容与主文件一致），然后把主文件写坏
    assert prefs.save(p, good)
    assert (tmp_path / (prefs.PREFS_FILENAME + ".bak")).is_file()
    p.write_text("{ not json", encoding="utf-8")

    restored = prefs.load(p)
    assert restored.snapshot_for("dep-a") == {"$\\ns\\v": "7"}
    assert p.read_text(encoding="utf-8") == "{ not json"


def test_load_without_usable_bak_preserves_and_rejects_corruption(tmp_path):
    p = tmp_path / prefs.PREFS_FILENAME
    p.write_text("[]", encoding="utf-8")  # 顶层不是对象
    with pytest.raises(ValueError, match="原文件已保留"):
        prefs.load(p)
    assert p.read_text(encoding="utf-8") == "[]"


def test_load_missing_file_returns_empty_store(tmp_path):
    assert prefs.load(tmp_path / "nope.json").mods == {}


# 同一中文命名空间分别以无 BOM 的 UTF-8 和本地代码页编码。
_SAMPLE_USER_INI_UTF8 = (
    b"; AUTOMATICALLY GENERATED FILE - DO NOT EDIT\r\n"
    b";\r\n"
    b"; This module will overwrite this file whenever any persistent settings are\r\n"
    b"; altered by hot key or command list.\r\n"
    b";\r\n"
    b"[Constants]\r\n"
    b"$\\runtime_state\\first_run = 0\r\n"
    b"$\\mods\\\xe9\xa1\xb5\\samplemerged-toggleh\\merged.ini\\swapvar = 1\r\n"
    b"$\\global\\region\\regioncheck = 2\r\n"
)

# 「页」的 GBK 字节 D2 B3 也构成合法 UTF-8，用于验证编码歧义下的等价匹配。
_SAMPLE_USER_INI_ANSI = _SAMPLE_USER_INI_UTF8.replace(b"\xe9\xa1\xb5", b"\xd2\xb3")

_TARGET_KEY = "$\\mods\\页\\samplemerged-toggleh\\merged.ini\\swapvar"


def _encoded_namespace_index():
    """构造包含中文部署名及大小写混合归档成员路径的命名空间索引。"""
    return build_attribution_index(
        [
            ModIniSource(
                mod_id="dep-sample",
                inis=(("SampleMerged-toggleH/merged.ini", "[Constants]\nglobal persist $swapvar = 0\n"),),
                implicit_prefix="mods\\页",
            )
        ]
    )


@pytest.mark.parametrize(
    ("raw", "expect_encoding"),
    [
        (_SAMPLE_USER_INI_UTF8, "utf-8"),
        (_SAMPLE_USER_INI_ANSI, "utf-8"),
    ],
)
def test_user_ini_attributes_regardless_of_writer(
    tmp_path,
    raw,
    expect_encoding,
):
    p = tmp_path / "d3dx_user.ini"
    p.write_bytes(raw)
    snap = mui.read_stable(p, settle_seconds=0, attempts=2)
    assert snap is not None
    assert snap.encoding == expect_encoding
    assert mui.encode_ini_text(snap.text, snap.encoding) == raw  # 字节级往返

    index = _encoded_namespace_index()
    hits = {k: index.resolve(k) for k in snap.values}
    assert sum(1 for v in hits.values() if v == "dep-sample") == 1, hits
    # 运行时全局变量不参与 Mod 归属匹配。
    assert index.resolve("$\\runtime_state\\first_run") is None
    assert index.resolve("$\\global\\region\\regioncheck") is None


@pytest.mark.parametrize("raw", [_SAMPLE_USER_INI_UTF8, _SAMPLE_USER_INI_ANSI])
def test_encoded_key_removal_hits_the_line(tmp_path, raw):
    text, encoding = mui.decode_ini_bytes(raw)
    stripped = mui.remove_keys(text, [_TARGET_KEY])
    assert "swapvar" not in stripped
    assert "$\\runtime_state\\first_run = 0" in stripped
    assert mui.encode_ini_text(stripped, encoding)  # 仍可按同一编码写回
