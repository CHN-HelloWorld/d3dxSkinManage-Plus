from __future__ import annotations

import pytest

from src.models.preset import Preset
from tests.helpers import apply_preset
from tests.helpers import registered_games as registered_games


def _seed_mod(pm, mid, cid, game_id="game_a"):
    from tests.helpers import make_mod

    index = pm._get_index()
    if cid:
        node_id = f"{game_id}-{cid}"
        index._db().execute(
            "INSERT OR IGNORE INTO nodes(id,game_id,directory_name) VALUES(?,?,?)", (node_id, game_id, cid)
        )
        index._db().commit()
    else:
        node_id = ""
    mod = make_mod(index, mid, mod_id=mid, node_id=node_id, game_id=game_id)
    assert index.add_mod(mod)
    mod.node_id = cid
    return mod


@pytest.fixture
def managed_library(temp_data_dir, tmp_path):
    from src.core.config import Config
    from src.core.mod_manager import ModManager

    Config().set("import.format", "zip")
    mm = ModManager()
    mm.set_game("game_a")
    mm.set_deploy_dir(str(tmp_path / "deploy"))
    yield mm
    mm.stop_ini_sync()


def _import_mod(mm, tmp_path, name, character_id):
    source = tmp_path / name
    source.mkdir()
    (source / "mod.ini").write_text(f"[Constants]\n; {name}\n", encoding="utf-8")
    mod = mm.import_mod(str(source), node_id=character_id)
    assert mod is not None
    return mod


class TestPresetDataModel:
    def test_preset_default_fields(self):
        p = Preset(id="x", name="N", game_id="game_a")
        assert p.character_mods == {}
        assert p.is_default is False
        assert p.description == ""

    def test_preset_to_dict_roundtrip(self):
        p = Preset(
            id="abc",
            name="测试组",
            description="desc",
            game_id="game_a",
            is_default=False,
            character_mods={
                "char-1": ["407104662c834224b2f85bd101234be6", "4c42969a72fa4f45ace0cc94086b1cc2"],
                "char-2": ["c1414097f7ba451bafc3411192995589"],
            },
        )
        data = p.to_dict()
        p2 = Preset.from_dict(data)
        assert p2.id == "abc"
        assert p2.name == "测试组"
        assert p2.is_default is False
        assert p2.character_mods == {
            "char-1": ["407104662c834224b2f85bd101234be6", "4c42969a72fa4f45ace0cc94086b1cc2"],
            "char-2": ["c1414097f7ba451bafc3411192995589"],
        }

    def test_preset_from_dict_rejects_incomplete_record(self):
        with pytest.raises(ValueError):
            Preset.from_dict({"id": "x", "name": "N"})


class TestGameStorePresetApi:
    """预设指针 2026-09-10 起归 `GameConfigStore`（游戏数据，不随「恢复默认」清零）。"""

    def test_default_current_preset_id_is_default_marker(self, temp_data_dir):
        from src.core.game_config_store import GameConfigStore

        store = GameConfigStore()
        assert store.get_current_preset_id("game_a") is None

    def test_set_and_get_current_preset_id(self, temp_data_dir):
        from src.core.game_config_store import GameConfigStore

        store = GameConfigStore()
        store.set_current_preset_id("game_a", "abc12345")
        assert store.get_current_preset_id("game_a") == "abc12345"

    def test_per_game_isolation(self, temp_data_dir):
        from src.core.game_config_store import GameConfigStore

        store = GameConfigStore()
        store.set_current_preset_id("game_a", "g-preset")
        store.set_current_preset_id("game_b", "s-preset")
        assert store.get_current_preset_id("game_a") == "g-preset"
        assert store.get_current_preset_id("game_b") == "s-preset"

    def test_set_persists_across_reload(self, temp_data_dir):
        from src.core.game_config_store import GameConfigStore

        store = GameConfigStore()
        store.set_current_preset_id("game_a", "abc")
        store.reload()
        assert store.get_current_preset_id("game_a") == "abc"


# 默认预设。


class TestPresetManagerEnsureDefault:
    def _make_pm(self):
        from src.core.preset_manager import PresetManager

        return PresetManager()

    def test_ensure_default_creates_once(self, temp_data_dir):
        pm = self._make_pm()
        d1 = pm.ensure_default("game_a")
        d2 = pm.ensure_default("game_a")
        assert d1.id == "default_game_a"
        assert d1.is_default is True
        assert d1.name == "默认"
        assert d1.game_id == "game_a"
        assert d2.id == d1.id

    def test_ensure_default_per_game(self, temp_data_dir):
        pm = self._make_pm()
        g = pm.ensure_default("game_a")
        s = pm.ensure_default("game_b")
        assert g.id == "default_game_a"
        assert s.id == "default_game_b"
        assert g.id != s.id

    def test_default_preset_persisted(self, temp_data_dir):
        pm1 = self._make_pm()
        pm1.ensure_default("game_a")
        pm2 = self._make_pm()
        d = pm2.get_preset("default_game_a")
        assert d is not None
        assert d.is_default is True


# 预设创建、读取、修改和删除。


class TestPresetCRUD:
    def _pm(self):
        from src.core.preset_manager import PresetManager

        return PresetManager()

    def test_create_preset_empty(self, temp_data_dir):
        pm = self._pm()
        p = pm.create_preset("game_a", "test", description="desc")
        assert p.id != ""
        assert not p.id.startswith("default_")
        assert p.name == "test"
        assert p.game_id == "game_a"
        assert p.is_default is False
        assert p.character_mods == {}

    def test_create_preset_from_snapshot(self, temp_data_dir):
        pm = self._pm()
        snapshot = {
            "char-1": ["407104662c834224b2f85bd101234be6"],
            "char-2": ["4c42969a72fa4f45ace0cc94086b1cc2", "c1414097f7ba451bafc3411192995589"],
        }
        for cid, mids in snapshot.items():
            for mid in mids:
                _seed_mod(pm, mid, cid)
        p = pm.create_preset("game_a", "snap", snapshot=snapshot)
        assert p.character_mods == snapshot
        snapshot["char-1"].append("X")
        assert "X" not in p.character_mods["char-1"]

    def test_delete_user_preset(self, temp_data_dir):
        pm = self._pm()
        p = pm.create_preset("game_a", "tmp")
        assert pm.delete_preset(p.id) is True
        assert pm.get_preset(p.id) is None

    def test_delete_default_preset_refused(self, temp_data_dir):
        pm = self._pm()
        d = pm.ensure_default("game_a")
        assert pm.delete_preset(d.id) is False
        assert pm.get_preset(d.id) is not None

    def test_update_preset_metadata(self, temp_data_dir):
        pm = self._pm()
        p = pm.create_preset("game_a", "old")
        assert pm.update_preset_meta(p.id, name="new", description="d") is True
        p2 = pm.get_preset(p.id)
        assert p2.name == "new" and p2.description == "d"

    def test_update_default_preset_name_allowed(self, temp_data_dir):
        pm = self._pm()
        d = pm.ensure_default("game_a")
        assert pm.update_preset_meta(d.id, name="我的默认") is True
        assert pm.get_preset(d.id).name == "我的默认"

    def test_update_empty_name_rejected(self, temp_data_dir):
        pm = self._pm()
        p = pm.create_preset("game_a", "x")
        assert pm.update_preset_meta(p.id, name="   ") is False
        assert pm.get_preset(p.id).name == "x"

    def test_generate_unique_name_no_conflict(self, temp_data_dir):
        pm = self._pm()
        assert pm.generate_unique_name("game_a", "新建预设") == "新建预设"

    def test_generate_unique_name_with_conflict(self, temp_data_dir):
        pm = self._pm()
        pm.create_preset("game_a", "新建预设")
        assert pm.generate_unique_name("game_a", "新建预设") == "新建预设 2"

    def test_generate_unique_name_multiple_conflict(self, temp_data_dir):
        pm = self._pm()
        pm.create_preset("game_a", "P")
        pm.create_preset("game_a", "P 2")
        pm.create_preset("game_a", "P 3")
        assert pm.generate_unique_name("game_a", "P") == "P 4"

    def test_generate_unique_name_copy_suffix(self, temp_data_dir):
        pm = self._pm()
        pm.create_preset("game_a", "测试 copy")
        # 复制冲突时累加数字
        assert pm.generate_unique_name("game_a", "测试 copy") == "测试 copy 2"

    def test_generate_unique_name_isolated_per_game(self, temp_data_dir):
        pm = self._pm()
        pm.create_preset("game_a", "X")
        # 不同游戏的同名不算冲突
        assert pm.generate_unique_name("game_b", "X") == "X"


# 状态同步与引用清理。


class TestSyncModState:
    def _pm(self):
        from src.core.preset_manager import PresetManager

        self.pm = PresetManager()
        return self.pm

    def _mod(self, mod_id, char_id, enabled):
        from src.models.mod import ModStatus

        mod = _seed_mod(self.pm, mod_id, char_id)
        mod.status = ModStatus.ENABLED if enabled else ModStatus.DISABLED
        return mod

    def test_sync_enable_adds_to_current(self, temp_data_dir):
        pm = self._pm()
        cur = pm.get_current("game_a")
        pm.sync_mod_state(self._mod("ca0df2c95aa144c190ff2ff3c8f967fd", "c1", True), game_id="game_a")
        assert pm.get_preset(cur.id).character_mods == {"c1": ["ca0df2c95aa144c190ff2ff3c8f967fd"]}

    def test_sync_disable_removes_from_current(self, temp_data_dir):
        pm = self._pm()
        cur = pm.get_current("game_a")
        pm.sync_mod_state(self._mod("ca0df2c95aa144c190ff2ff3c8f967fd", "c1", True), game_id="game_a")
        pm.sync_mod_state(self._mod("ca0df2c95aa144c190ff2ff3c8f967fd", "c1", False), game_id="game_a")
        assert "c1" not in pm.get_preset(cur.id).character_mods

    def test_sync_dedup(self, temp_data_dir):
        pm = self._pm()
        cur = pm.get_current("game_a")
        pm.sync_mod_state(self._mod("ca0df2c95aa144c190ff2ff3c8f967fd", "c1", True), game_id="game_a")
        pm.sync_mod_state(self._mod("ca0df2c95aa144c190ff2ff3c8f967fd", "c1", True), game_id="game_a")
        assert pm.get_preset(cur.id).character_mods["c1"] == ["ca0df2c95aa144c190ff2ff3c8f967fd"]

    def test_sync_multiple_mods_same_character(self, temp_data_dir):
        pm = self._pm()
        cur = pm.get_current("game_a")
        pm.sync_mod_state(self._mod("ca0df2c95aa144c190ff2ff3c8f967fd", "c1", True), game_id="game_a")
        pm.sync_mod_state(self._mod("29c1b289e7524195b362e44f54e05470", "c1", True), game_id="game_a")
        assert pm.get_preset(cur.id).character_mods["c1"] == [
            "ca0df2c95aa144c190ff2ff3c8f967fd",
            "29c1b289e7524195b362e44f54e05470",
        ]

    def test_sync_skips_mod_without_character_id(self, temp_data_dir):
        pm = self._pm()
        cur = pm.get_current("game_a")
        pm.sync_mod_state(self._mod("ca0df2c95aa144c190ff2ff3c8f967fd", "", True), game_id="game_a")
        assert pm.get_preset(cur.id).character_mods == {}

    def test_strip_mod_ids(self, temp_data_dir):
        pm = self._pm()
        cur = pm.get_current("game_a")
        pm.sync_mod_state(self._mod("ca0df2c95aa144c190ff2ff3c8f967fd", "c1", True), game_id="game_a")
        pm.sync_mod_state(self._mod("29c1b289e7524195b362e44f54e05470", "c1", True), game_id="game_a")
        pm.strip_mod_ids(["ca0df2c95aa144c190ff2ff3c8f967fd", "29c1b289e7524195b362e44f54e05470"], game_id="game_a")
        assert pm.get_preset(cur.id).character_mods == {}


# 级联清理。


class TestCascade:
    def _pm(self):
        from src.core.preset_manager import PresetManager

        pm = PresetManager()
        for mid, cid in (
            ("ca0df2c95aa144c190ff2ff3c8f967fd", "c1"),
            ("29c1b289e7524195b362e44f54e05470", "c2"),
            ("d9298a10d1b04358b7dc4bd85dac641b", "c1"),
        ):
            _seed_mod(pm, mid, cid)
        _seed_mod(pm, "7142a98f42394961a44c4dcde4d24cfc", "c1", "game_b")
        return pm

    def test_cascade_delete_mod(self, temp_data_dir):
        pm = self._pm()
        p1 = pm.create_preset(
            "game_a", "p1", snapshot={"c1": ["ca0df2c95aa144c190ff2ff3c8f967fd", "29c1b289e7524195b362e44f54e05470"]}
        )
        p2 = pm.create_preset("game_a", "p2", snapshot={"c1": ["ca0df2c95aa144c190ff2ff3c8f967fd"]})
        pm.cascade_delete_mod("ca0df2c95aa144c190ff2ff3c8f967fd", "game_a")
        assert pm.get_preset(p1.id).character_mods == {"c1": ["29c1b289e7524195b362e44f54e05470"]}
        assert pm.get_preset(p2.id).character_mods == {}

    def test_cascade_delete_character(self, temp_data_dir):
        pm = self._pm()
        p = pm.create_preset(
            "game_a",
            "p",
            snapshot={"c1": ["ca0df2c95aa144c190ff2ff3c8f967fd"], "c2": ["29c1b289e7524195b362e44f54e05470"]},
        )
        pm.cascade_delete_character("c1", "game_a")
        assert pm.get_preset(p.id).character_mods == {"c2": ["29c1b289e7524195b362e44f54e05470"]}

    def test_cascade_only_affects_same_game(self, temp_data_dir):
        pm = self._pm()
        p_g = pm.create_preset("game_a", "g", snapshot={"c1": ["ca0df2c95aa144c190ff2ff3c8f967fd"]})
        p_s = pm.create_preset("game_b", "s", snapshot={"c1": ["7142a98f42394961a44c4dcde4d24cfc"]})
        pm.cascade_delete_mod("ca0df2c95aa144c190ff2ff3c8f967fd", "game_a")
        assert pm.get_preset(p_g.id).character_mods == {}
        assert pm.get_preset(p_s.id).character_mods == {"c1": ["7142a98f42394961a44c4dcde4d24cfc"]}

    def test_cascade_move_mod(self, temp_data_dir):
        pm = self._pm()
        p = pm.create_preset("game_a", "p", snapshot={"c1": ["ca0df2c95aa144c190ff2ff3c8f967fd"]})
        pm.cascade_move_mod("ca0df2c95aa144c190ff2ff3c8f967fd", "c1", "c2", "game_a")
        assert pm.get_preset(p.id).character_mods == {"c2": ["ca0df2c95aa144c190ff2ff3c8f967fd"]}

    def test_cascade_move_mod_when_not_in_preset(self, temp_data_dir):
        pm = self._pm()
        p = pm.create_preset("game_a", "p", snapshot={"c1": ["d9298a10d1b04358b7dc4bd85dac641b"]})
        pm.cascade_move_mod("ca0df2c95aa144c190ff2ff3c8f967fd", "c1", "c2", "game_a")
        assert pm.get_preset(p.id).character_mods == {"c1": ["d9298a10d1b04358b7dc4bd85dac641b"]}


# 应用预设。


class TestApplyPreset:
    """通过真实索引和部署目录验证预设的规划、日志、文件执行及提交事务。"""

    def _pm(self, mod_index, mod_storage):
        from src.core.preset_manager import PresetManager

        return PresetManager(mod_index=mod_index, mod_storage=mod_storage)

    def _make_env(self, tmp_path, mods_state):
        """根据 mods_state 中的角色及启用状态构造索引、存储和 Mod 记录。

        部署接口要求归档本体，因此使用压缩包构造部署源。
        """
        from src.core.mod_index import ModIndex
        from src.core.mod_storage import ModStorage
        from src.models.mod import Mod
        from tests.core.test_preset_deployment import _archive_source

        idx = ModIndex("game_a")
        storage = ModStorage()
        storage.set_migoto_mods_dir(tmp_path / "Mods")

        from src.models.deployment import DeploymentRecord, DeploymentState
        from tests.helpers import make_mod

        mods = {}
        for mid, (cid, enabled) in mods_state.items():
            idx._db().execute(
                "INSERT OR IGNORE INTO nodes(id,game_id,directory_name) VALUES(?,?,?)", (cid, "game_a", cid)
            )
            idx._db().commit()
            node_root = idx.paths.node_path(cid)
            node_root.mkdir(parents=True, exist_ok=True)
            source = _archive_source(node_root, mid, {"mod.ini": f"[Constants]\nx={mid}\n".encode()}, mid)
            mod = make_mod(
                idx, mid, mod_id=mid, node_id=cid, body_name=source.name, content_hash=Mod.compute_sha1(source)
            )
            assert idx.add_mod(mod)
            if enabled:
                deployed = storage.materialize_deployment(mod.path, mod.mod_id, mid)
                idx.upsert_deployment(DeploymentRecord(mid, DeploymentState.ACTIVE, str(deployed)))
                mod = idx.get_mod(mid)
            mods[mid] = mod
        return idx, storage, mods

    def test_apply_full_reset_disables_unlisted(self, temp_data_dir, tmp_path):
        idx, storage, _ = self._make_env(
            tmp_path,
            {
                "ca0df2c95aa144c190ff2ff3c8f967fd": ("c1", True),
                "29c1b289e7524195b362e44f54e05470": ("c2", False),
            },
        )
        pm = self._pm(idx, storage)
        target = pm.create_preset("game_a", "t", snapshot={"c2": ["29c1b289e7524195b362e44f54e05470"]})

        affected = apply_preset(pm, target.id)

        assert set(affected) == {"ca0df2c95aa144c190ff2ff3c8f967fd", "29c1b289e7524195b362e44f54e05470"}
        assert idx.get_mod("ca0df2c95aa144c190ff2ff3c8f967fd").is_enabled is False
        assert idx.get_mod("29c1b289e7524195b362e44f54e05470").is_enabled is True
        active_m2 = storage.deployment_paths(
            idx.get_mod("29c1b289e7524195b362e44f54e05470").mod_id, "29c1b289e7524195b362e44f54e05470"
        )[0]
        assert active_m2.exists(), "目标预设里的 Mod 必须真的部署出去"

    def test_apply_writes_current_preset_id(self, temp_data_dir, tmp_path):
        idx, storage, _ = self._make_env(tmp_path, {})
        pm = self._pm(idx, storage)
        from src.core.game_config_store import GameConfigStore

        target = pm.create_preset("game_a", "t")

        apply_preset(pm, target.id)

        assert GameConfigStore().get_current_preset_id("game_a") == target.id

    def test_apply_nonexistent_returns_empty(self, temp_data_dir, tmp_path):
        idx, storage, _ = self._make_env(tmp_path, {})
        pm = self._pm(idx, storage)

        assert apply_preset(pm, "does-not-exist") == []

    def test_apply_no_change_if_states_match(self, temp_data_dir, tmp_path):
        idx, storage, mods = self._make_env(tmp_path, {"ca0df2c95aa144c190ff2ff3c8f967fd": ("c1", True)})
        pm = self._pm(idx, storage)
        target = pm.create_preset("game_a", "t", snapshot={"c1": ["ca0df2c95aa144c190ff2ff3c8f967fd"]})
        deployed_before = mods["ca0df2c95aa144c190ff2ff3c8f967fd"].deployed_path

        affected = apply_preset(pm, target.id)

        assert affected == []
        assert idx.get_mod("ca0df2c95aa144c190ff2ff3c8f967fd").is_enabled is True
        assert idx.get_mod("ca0df2c95aa144c190ff2ff3c8f967fd").deployed_path == deployed_before, (
            "状态一致时不该重新部署"
        )


# 启用及批量禁用事件。


class TestModManagerIntegration:
    """通过真实导入、索引与部署验证预设同步钩子。"""

    def test_toggle_mod_writes_current_preset(self, managed_library, tmp_path):
        mm = managed_library
        char = mm.add_character(name="First", node_type="character")
        mod = _import_mod(mm, tmp_path, "Skin", char.id)
        assert mm.toggle_mod(mod.mod_id).is_enabled
        assert mm.preset_manager.get_current("game_a").character_mods == {char.id: [mod.mod_id]}
        assert not mm.toggle_mod(mod.mod_id).is_enabled
        assert mm.preset_manager.get_current("game_a").character_mods == {}

    def test_disable_all_clears_current_preset(self, managed_library, tmp_path):
        mm = managed_library
        for name in ("First", "Second"):
            char = mm.add_character(name=name, node_type="character")
            mod = _import_mod(mm, tmp_path, name, char.id)
            assert mm.toggle_mod(mod.mod_id).is_enabled
        assert mm.disable_all_mods() == 2
        assert mm.preset_manager.get_current("game_a").character_mods == {}
        assert all(not mod.is_enabled for mod in mm.get_mods())

    def test_disable_all_by_character_removes_only_that_key(self, managed_library, tmp_path):
        mm = managed_library
        first = mm.add_character(name="First", node_type="character")
        second = mm.add_character(name="Second", node_type="character")
        a = _import_mod(mm, tmp_path, "SkinA", first.id)
        b = _import_mod(mm, tmp_path, "SkinB", second.id)
        assert mm.toggle_mod(a.mod_id).is_enabled
        assert mm.toggle_mod(b.mod_id).is_enabled
        assert mm.disable_all_mods(node_id=first.id) == 1
        assert mm.preset_manager.get_current("game_a").character_mods == {second.id: [b.mod_id]}
        assert mm.get_mod(b.mod_id).is_enabled


class TestModManagerCascade:
    def test_delete_mod_cascades_to_presets(self, managed_library, tmp_path):
        mm = managed_library
        char = mm.add_character(name="First", node_type="character")
        a = _import_mod(mm, tmp_path, "SkinA", char.id)
        b = _import_mod(mm, tmp_path, "SkinB", char.id)
        preset = mm.preset_manager.create_preset("game_a", "Custom", snapshot={char.id: [a.mod_id, b.mod_id]})
        assert mm.delete_mod(a.mod_id, delete_method="permanent")
        assert preset.character_mods == {char.id: [b.mod_id]}
        assert mm.get_mod(a.mod_id) is None

    def test_move_mod_cascades(self, managed_library, tmp_path):
        mm = managed_library
        first = mm.add_character(name="First", node_type="character")
        second = mm.add_character(name="Second", node_type="character")
        mod = _import_mod(mm, tmp_path, "Skin", first.id)
        preset = mm.preset_manager.create_preset("game_a", "Custom", snapshot={first.id: [mod.mod_id]})
        assert mm.move_mod(mod.mod_id, second.id)
        assert preset.character_mods == {second.id: [mod.mod_id]}
        assert mm.get_mod(mod.mod_id).node_id == second.id

    def test_delete_character_cascades(self, managed_library, tmp_path):
        mm = managed_library
        first = mm.add_character(name="First", node_type="character")
        second = mm.add_character(name="Second", node_type="character")
        a = _import_mod(mm, tmp_path, "SkinA", first.id)
        b = _import_mod(mm, tmp_path, "SkinB", second.id)
        preset = mm.preset_manager.create_preset(
            "game_a", "Custom", snapshot={first.id: [a.mod_id], second.id: [b.mod_id]}
        )
        assert mm.delete_character(first.id)
        assert first.id not in preset.character_mods
        assert preset.character_mods[second.id] == [b.mod_id]

    def test_enforce_single_mod_strips_preset(self, managed_library, tmp_path):
        mm = managed_library
        char = mm.add_character(name="First", node_type="character")
        a = _import_mod(mm, tmp_path, "SkinA", char.id)
        b = _import_mod(mm, tmp_path, "SkinB", char.id)
        assert mm.toggle_mod(a.mod_id).is_enabled
        assert mm.enforce_single_mod(b.mod_id) == [a.mod_id]
        assert a.mod_id not in mm.preset_manager.get_current("game_a").enabled_mod_ids
        assert not mm.get_mod(a.mod_id).is_enabled
