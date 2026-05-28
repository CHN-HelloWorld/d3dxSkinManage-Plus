from __future__ import annotations

import json

import pytest

IDENTITY = "aaaaaaaaaaaa4aaa8aaaaaaaaaaaaaaa"


def _presets_path(temp_data_dir):
    return temp_data_dir / "data" / "presets.json"


def _write(temp_data_dir, payload) -> None:
    p = _presets_path(temp_data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _invalid_backups(temp_data_dir) -> list:
    return sorted((temp_data_dir / "data").glob("presets.json.invalid-*"))


def _valid_record(**over) -> dict:
    rec = {
        "id": "p1",
        "name": "组一",
        "description": "",
        "game_id": "game_a",
        "is_default": False,
        "character_mods": {"c1": [IDENTITY]},
        "mod_variants": {IDENTITY: None},
        "created_at": "2026-01-01T00:00:00",
        "updated_at": "2026-01-01T00:00:00",
    }
    rec.update(over)
    return rec


class TestPresetFromDictStrict:
    def test_valid_roundtrip(self):
        from src.models.preset import Preset

        p = Preset.from_dict(_valid_record())
        assert p.id == "p1"
        assert p.character_mods == {"c1": [IDENTITY]}
        assert p.to_dict() == _valid_record()

    @pytest.mark.parametrize(
        "record, reason",
        [
            ("not a dict", "记录不是对象"),
            ({}, "全空"),
            (_valid_record(id=""), "id 为空"),
            (_valid_record(id=123), "id 非字符串"),
            (_valid_record(name=""), "name 为空"),
            (_valid_record(game_id=""), "game_id 为空"),
            (_valid_record(character_mods=["m1"]), "character_mods 不是映射"),
            (_valid_record(character_mods={"c1": "m1"}), "character_mods 值不是列表"),
            (_valid_record(character_mods={"c1": [1]}), "mod_id 非字符串"),
            (_valid_record(is_default="yes"), "is_default 非布尔"),
            (_valid_record(is_default=1), "is_default 用 int 冒充布尔"),
            (_valid_record(created_at=0), "created_at 非字符串"),
            (_valid_record(description=None), "description 非字符串"),
        ],
    )
    def test_invalid_record_raises(self, record, reason):
        from src.models.preset import Preset

        with pytest.raises(ValueError):
            Preset.from_dict(record)

    def test_missing_identity_field_raises(self):
        from src.models.preset import Preset

        for missing in ("id", "name", "game_id", "character_mods"):
            record = _valid_record()
            record.pop(missing)
            with pytest.raises(ValueError):
                Preset.from_dict(record)

    def test_missing_metadata_field_uses_default(self):
        from src.models.preset import Preset

        record = _valid_record()
        for k in ("is_default", "created_at", "updated_at", "description"):
            record.pop(k)
        p = Preset.from_dict(record)
        assert p.is_default is False
        assert p.description == ""
        assert p.created_at and p.updated_at


class TestPresetFileSchema:
    def _load(self):
        from src.core.preset_manager import PresetManager

        return PresetManager()

    def test_valid_v2_loads(self, temp_data_dir):
        _write(temp_data_dir, {"version": 2, "presets": [_valid_record()]})
        pm = self._load()
        assert pm.get_preset("p1").character_mods == {}
        assert pm._durable_refs["p1"] == {"c1": [IDENTITY]}
        assert not _invalid_backups(temp_data_dir)

    def test_missing_file_is_not_an_error(self, temp_data_dir):
        pm = self._load()
        assert pm.get_preset("p1") is None
        assert not _invalid_backups(temp_data_dir)

    def test_missing_version_still_loads(self, temp_data_dir):
        _write(temp_data_dir, {"presets": [_valid_record()]})
        pm = self._load()
        assert pm.get_preset("p1") is not None
        assert not _invalid_backups(temp_data_dir)

    def test_v1_records_are_skipped_without_invalidating_the_file(self, temp_data_dir):
        _write(
            temp_data_dir,
            {
                "version": 1,
                "presets": [
                    {"id": "old", "name": "旧", "game_id": "game_a", "items": [{"mod_id": "m1", "enabled": True}]},
                    _valid_record(),
                ],
            },
        )
        pm = self._load()
        assert pm.get_preset("old") is None
        assert pm.get_preset("p1") is not None, "同一份文件里合法的记录必须照常加载"
        assert not _invalid_backups(temp_data_dir)

    def test_legacy_items_key_is_ignored_not_fatal(self, temp_data_dir):
        _write(
            temp_data_dir,
            {
                "version": 2,
                "presets": [_valid_record(), _valid_record(id="p2", items=[])],
            },
        )
        pm = self._load()
        assert pm.get_preset("p1") is not None
        assert pm.get_preset("p2") is not None
        assert not _invalid_backups(temp_data_dir)

    def test_future_version_still_loads(self, temp_data_dir):
        _write(temp_data_dir, {"version": 999, "presets": [_valid_record()]})
        pm = self._load()
        assert pm.get_preset("p1").character_mods == {}
        assert pm._durable_refs["p1"] == {"c1": [IDENTITY]}
        assert not _invalid_backups(temp_data_dir)

    def test_one_bad_record_only_drops_itself(self, temp_data_dir):
        _write(
            temp_data_dir,
            {
                "version": 2,
                "presets": [_valid_record(), _valid_record(id="p2", game_id="")],
            },
        )
        pm = self._load()
        assert pm.get_preset("p1") is not None
        assert pm.get_preset("p2") is None
        assert not _invalid_backups(temp_data_dir)

    def test_duplicate_id_keeps_the_first_record(self, temp_data_dir):
        _write(
            temp_data_dir,
            {
                "version": 2,
                "presets": [_valid_record(name="先出现"), _valid_record(name="后出现")],
            },
        )
        pm = self._load()
        assert pm.get_preset("p1").name == "先出现"
        assert not _invalid_backups(temp_data_dir)

    def test_broken_json_invalidates_whole_file(self, temp_data_dir):
        p = _presets_path(temp_data_dir)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('{"version": 2, "presets": [', encoding="utf-8")
        pm = self._load()
        assert pm.get_preset("p1") is None
        assert len(_invalid_backups(temp_data_dir)) == 1

    def test_presets_not_a_list_invalidates_whole_file(self, temp_data_dir):
        _write(temp_data_dir, {"version": 2, "presets": {"p1": _valid_record()}})
        self._load()
        assert len(_invalid_backups(temp_data_dir)) == 1

    def test_backup_preserves_original_bytes(self, temp_data_dir):
        payload = {"version": 2, "presets": {"p1": _valid_record()}}  # presets 不是列表
        _write(temp_data_dir, payload)
        original = _presets_path(temp_data_dir).read_text(encoding="utf-8")
        self._load()
        backups = _invalid_backups(temp_data_dir)
        assert backups[0].read_text(encoding="utf-8") == original
        assert not _presets_path(temp_data_dir).exists(), "无效文件应被改名让位，而非留在原地反复告警"

    def test_migration_helpers_are_gone(self, temp_data_dir):
        from src.core.preset_manager import PresetManager

        assert not hasattr(PresetManager, "_migrate_v1_to_v2")
        assert not hasattr(PresetManager, "_backup_v1")

    def test_save_after_invalid_writes_clean_v2(self, temp_data_dir):
        from src.core.preset_manager import SCHEMA_VERSION

        _write(temp_data_dir, {"version": 2, "presets": {"p1": _valid_record()}})
        pm = self._load()
        pm.ensure_default("game_a")
        data = json.loads(_presets_path(temp_data_dir).read_text(encoding="utf-8"))
        assert data["version"] == SCHEMA_VERSION
        assert [p["id"] for p in data["presets"]] == ["default_game_a"]
