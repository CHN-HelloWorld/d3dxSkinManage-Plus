from __future__ import annotations

from pathlib import Path

import pytest

from src.core.ini_keys import (
    IniWriteError,
    extract_key_names,
    parse_ini_text,
    parse_mod_keys,
    rewrite_key_in_text,
)


def test_combination_replaces_conflicting_modifier_exclusions():
    from src.core.ini_keys import rewrite_key_in_text

    source = "[KeySwap]\r\nkey = x no_modifiers ; keep\r\nback = z\r\n"
    assert rewrite_key_in_text(source, 1, "key", "ctrl alt k") == (
        "[KeySwap]\r\nkey = ctrl alt k no_shift no_lwin no_rwin ; keep\r\nback = z\r\n"
    )


def test_hex_keyboard_keys_match_named_exclusions():
    from src.core.ini_keys import rewrite_key_in_text

    assert rewrite_key_in_text("key = x NO_VK_OEM_PERIOD\n", 0, "key", "0xBE") == "key = 0xBE\n"
    assert rewrite_key_in_text("key = x no_modifiers\n", 0, "key", "0x11 k") == (
        "key = 0x11 k no_alt no_shift no_lwin no_rwin\n"
    )


def test_controller_trigger_threshold_is_not_lost():
    from src.core.ini_keys import parse_ini_text, rewrite_key_in_text

    source = "[KeySwap]\nkey = NO_XB2_LEFT_TRIGGER > 128\n"
    binding = parse_ini_text(source, "mod.ini")[0]
    assert "128" in binding.key_flags
    edited = rewrite_key_in_text(source, 1, "key", "ctrl k")
    assert "NO_XB2_LEFT_TRIGGER>128" in edited


def test_combination_keeps_unrelated_exclusions():
    from src.core.ini_keys import rewrite_key_in_text

    source = "[KeySwap]\nkey = x no_ctrl no_shift\n"
    assert rewrite_key_in_text(source, 1, "key", "ctrl k j") == ("[KeySwap]\nkey = ctrl k j no_shift\n")


def test_prefix_exclusions_survive_combination_edit_and_parse_again():
    from src.core.ini_keys import parse_ini_text, rewrite_key_in_text

    source = "[KeySwap]\nkey = no_modifiers NO_VK_DECIMAL VK_NUMPAD0\n"
    edited = rewrite_key_in_text(source, 1, "key", "ctrl VK_NUMPAD1")
    binding = parse_ini_text(edited, "mod.ini")[0]
    assert binding.key_value == "ctrl VK_NUMPAD1"
    assert binding.key_flags == "no_alt no_shift no_lwin no_rwin NO_VK_DECIMAL"


SAMPLE = """\
; 头部注释
[KeySwap]
key = y                    ; 前进键
back = VK_DOWN
type = cycle
$swapvar = 0,1,2,3
condition = $active == 1

[KeyToggle]
key = VK_F10 no_modifiers
"""


def test_parse_finds_two_sections():
    bs = parse_ini_text(SAMPLE, "merged.ini")
    assert [b.section for b in bs] == ["KeySwap", "KeyToggle"]


def test_parse_keyswap_fields():
    b = parse_ini_text(SAMPLE, "merged.ini")[0]
    assert b.ini_path == "merged.ini"
    assert b.key_value == "y"
    assert b.key_flags == ""
    assert b.key_line_no == 2  # 行号从零开始，依次为注释、节标题和键定义。
    assert b.back_value == "VK_DOWN"
    assert b.back_line_no == 3
    assert b.type == "cycle"
    assert b.var_name == "$swapvar"
    assert b.cycle_values == "0,1,2,3"
    assert b.condition == "$active == 1"


def test_parse_strips_inline_comment_and_keeps_tail_flag():
    b = parse_ini_text(SAMPLE, "merged.ini")[1]
    assert b.key_value == "VK_F10"  # 注释剥离
    assert b.key_flags == "no_modifiers"
    assert b.back_value is None and b.back_line_no is None


def test_parse_ignores_keyword_lookalikes():
    txt = "[S]\nkeyword = nope\nkey_extra = nope\nkey = a\n"
    bs = parse_ini_text(txt, "x.ini")
    assert len(bs) == 1 and bs[0].key_value == "a" and bs[0].key_line_no == 3


def test_parse_section_without_key_is_dropped():
    txt = "[NoKey]\ntype = cycle\n[HasKey]\nkey = h\n"
    bs = parse_ini_text(txt, "x.ini")
    assert [b.section for b in bs] == ["HasKey"]


def test_extract_key_names_dedupes_preserving_order():
    bs = parse_ini_text(SAMPLE, "m.ini")
    assert extract_key_names(bs) == ["y", "VK_DOWN", "VK_F10"]


def test_parse_mod_keys_walks_folder(tmp_path):
    (tmp_path / "a.ini").write_text("[K]\nkey = y\n", encoding="utf-8")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "b.ini").write_text("[K]\nkey = h\n", encoding="utf-8")
    bs = parse_mod_keys(str(tmp_path))
    assert {b.key_value for b in bs} == {"y", "h"}
    assert all(Path(b.ini_path).is_absolute() for b in bs)


def test_parse_mod_keys_missing_path_returns_empty():
    assert parse_mod_keys(str(Path("definitely") / "missing")) == []


def test_parse_mod_keys_orders_outer_before_inner(tmp_path):
    # 最外层 ini（用户最想先看到的，如 MasterIota.ini）
    (tmp_path / "MasterIota.ini").write_text("[K]\nkey = m\n", encoding="utf-8")
    # 子目录名称的字母顺序在前，但较深的 INI 仍应排在根目录 INI 之后。
    aaa = tmp_path / "AAA"
    aaa.mkdir()
    (aaa / "inner.ini").write_text("[K]\nkey = a\n", encoding="utf-8")
    deeper = aaa / "deeper"
    deeper.mkdir()
    (deeper / "deep.ini").write_text("[K]\nkey = d\n", encoding="utf-8")

    names = [Path(b.ini_path).name for b in parse_mod_keys(str(tmp_path))]
    assert names == ["MasterIota.ini", "inner.ini", "deep.ini"]  # 外 → 内


# 行级修改保留其余内容及原始换行。

WRITE_SAMPLE = "[KeySwap]\r\nkey = y                    ; 前进键\r\nback = VK_DOWN\r\n"


def test_rewrite_key_changes_only_token():
    binding = parse_ini_text(WRITE_SAMPLE, "merged.ini")[0]
    out = rewrite_key_in_text(WRITE_SAMPLE, binding.key_line_no, "key", "h")
    # 仅替换键值，保留缩进、对齐、行尾注释、CRLF 和其他行。
    assert out == "[KeySwap]\r\nkey = h                    ; 前进键\r\nback = VK_DOWN\r\n"


def test_rewrite_key_preserves_trailing_flag():
    source = "[K]\nkey = VK_F10 no_modifiers\n"
    binding = parse_ini_text(source, "mod.ini")[0]
    assert rewrite_key_in_text(source, binding.key_line_no, "key", "VK_F9") == "[K]\nkey = VK_F9 no_modifiers\n"


def test_rewrite_back_field():
    source = "[K]\nkey = y\nback = VK_DOWN\n"
    binding = parse_ini_text(source, "mod.ini")[0]
    assert rewrite_key_in_text(source, binding.back_line_no, "back", "VK_UP") == "[K]\nkey = y\nback = VK_UP\n"


def test_rewrite_raises_when_line_changed_underneath():
    binding = parse_ini_text("[K]\nkey = y\n", "mod.ini")[0]
    changed = "[K]\n; key line gone\n"
    with pytest.raises(IniWriteError):
        rewrite_key_in_text(changed, binding.key_line_no, "key", "h")
