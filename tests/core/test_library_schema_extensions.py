"""验证不同 schema 版本及未知字段在完整资源库操作中保持可用。

容器标记、类别标记和分享元数据同时包含扩展字段时，资源、分类层次和元数据均应保留；
重命名操作也不得丢失未知字段。
"""

from __future__ import annotations

import json
from pathlib import Path

from src.core.mod_manager import ModManager
from src.core.mod_package import META_DIR, MOD_CONTAINER_MARKER, read_sidecar
from src.models.mod import Mod
from src.utils.constants import CATEGORY_MARKER_FILE
from tests.helpers import register_games, reset_game_service
from tests.helpers import registered_games as registered_games


def test_future_schema_library_is_fully_usable(temp_data_dir, tmp_path, capsys):
    register_games("game_a")
    ModManager._instance = None
    try:
        mm = ModManager()
        mm.set_game("game_a")
        char = mm.add_character(name="示例角色丁", node_type="character")
        src = tmp_path / "Skin"
        src.mkdir()
        (src / "mod.ini").write_text("[Constants]\nx=1\n", encoding="utf-8")
        mod = mm.import_mod(str(src), node_id=char.id, name="示例角色丁·旗袍")
        assert mod is not None
        mod.info.author = "Alice"
        assert mm.update_mod(mod)

        container = Path(mod.path).parent
        # 将容器标记、类别标记和分享元数据的 schema 设置为 999，并添加未知字段。
        marker_path = container / MOD_CONTAINER_MARKER
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        marker["schema"] = 999
        marker["future_marker_field"] = "x"
        marker_path.write_text(json.dumps(marker, ensure_ascii=False), encoding="utf-8")

        info_path = container / META_DIR / "info.json"
        sidecar = json.loads(info_path.read_text(encoding="utf-8"))
        sidecar["schema"] = 999
        sidecar["future_top"] = {"a": 1}
        sidecar.setdefault("local", {})["future_local"] = ["x"]
        info_path.write_text(json.dumps(sidecar, ensure_ascii=False), encoding="utf-8")

        cat_path = mm._get_character_library(char.id) / CATEGORY_MARKER_FILE
        cat = json.loads(cat_path.read_text(encoding="utf-8"))
        cat["schema"] = 999
        cat["future_cat_field"] = 7
        cat_path.write_text(json.dumps(cat, ensure_ascii=False), encoding="utf-8")

        # ② 重开程序：角色树 + Mod + 元数据必须都还在
        ModManager._instance = None
        mm2 = ModManager()
        mm2.set_game("game_a")
        assert [c.name for c in mm2.get_characters("game_a")] == ["示例角色丁"]
        found = mm2.get_mods(node_id=char.id)
        assert [m.display_name for m in found] == ["示例角色丁·旗袍"]
        assert read_sidecar(found[0].path)["info"]["author"] == "Alice"

        # ③ 从 marker 重建角色树也认得未来版本的 marker
        mm2._index.clear_mods_for_game("game_a")
        mm2._index._db().execute("DELETE FROM nodes")
        mm2._index._db().commit()
        assert mm2.rebuild_character_tree_from_markers("game_a") == 1

        mm2.rebuild_index_from_filesystem("game_a")
        # ④ 未知字段改一次名后仍在
        renamed = mm2.rename_mod(found[0].mod_id, "新名", sync_folder=True)
        assert renamed is not None
        sc = read_sidecar(renamed.path)
        assert sc["future_top"] == {"a": 1}
        assert sc["local"]["future_local"] == ["x"]
        assert json.loads(
            (Path(renamed.path).parent / MOD_CONTAINER_MARKER).read_text(encoding="utf-8"),
        )["content_hash"] == Mod.compute_sha1(renamed.path)
    finally:
        ModManager._instance = None
        reset_game_service()


def test_broken_preset_record_only_drops_itself(temp_data_dir):
    from src.core.preset_manager import PresetManager

    presets = temp_data_dir / "data" / "presets.json"
    presets.parent.mkdir(parents=True, exist_ok=True)
    presets.write_text(
        json.dumps(
            {
                "version": 999,
                "presets": [
                    {
                        "id": "good",
                        "name": "好的",
                        "game_id": "game_a",
                        "character_mods": {"c1": ["11111111111141118111111111111111"]},
                        "future": 1,
                    },
                    {"id": "bad", "name": "坏的", "game_id": "game_a"},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    pm = PresetManager()
    assert pm.get_preset("good") is not None
    assert pm.get_preset("bad") is None
    assert not list((temp_data_dir / "data").glob("presets.json.invalid-*"))
