from __future__ import annotations

from src.core.key_tokens import friendly_name, split_key_expression


def test_split_plain_token():
    assert split_key_expression("y") == ("y", "")
    assert split_key_expression("VK_DOWN") == ("VK_DOWN", "")
    assert split_key_expression("ctrl >") == ("ctrl >", "")


def test_split_peels_trailing_no_flags():
    assert split_key_expression("VK_F10 no_modifiers") == ("VK_F10", "no_modifiers")
    assert split_key_expression("x no_alt no_ctrl") == ("x", "no_alt no_ctrl")


def test_split_keeps_modifier_prefix_in_display():
    # ctrl/alt 是前缀修饰，属于用户在编辑的键表达式，不剥离
    assert split_key_expression("ctrl alt x") == ("ctrl alt x", "")
    assert split_key_expression("ctrl x no_modifiers") == ("ctrl x", "no_modifiers")


def test_split_empty():
    assert split_key_expression("") == ("", "")
    assert split_key_expression("   ") == ("", "")


def test_friendly_name_maps_known_vk():
    assert friendly_name("VK_DOWN") == "↓ 方向键下"
    assert friendly_name("vk_down") == "↓ 方向键下"  # 大小写不敏感


def test_friendly_name_single_letter_uppercases():
    assert friendly_name("y") == "Y"


def test_friendly_name_modifier_combo():
    assert friendly_name("ctrl alt x") == "Ctrl + Alt + X"


def test_friendly_name_unknown_token_unchanged():
    assert friendly_name("xinput_a") == "xinput_a"
    assert friendly_name("") == ""
