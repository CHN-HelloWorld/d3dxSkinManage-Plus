from __future__ import annotations


def test_import_detect_keys(tmp_path):
    (tmp_path / "m.ini").write_text("[K]\nkey = h\n", encoding="utf-8")
    from src.ui.dialogs.mod_import import _detect_keys

    assert set(_detect_keys(tmp_path)) == {"h"}
