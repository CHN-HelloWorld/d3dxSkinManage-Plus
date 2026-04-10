import json
import sqlite3

import pytest

from src.core.game_config_store import GameConfigStore
from src.core.mod_index import ModIndex
from src.models.deployment import DeploymentRecord, DeploymentState
from src.models.mod import Mod


@pytest.fixture
def index(temp_data_dir):
    GameConfigStore.instance().set_games(
        [{"id": "g", "name": "Game", "name_en": "Game", "mods_path": "", "resource_dir": "resources"}]
    )
    value = ModIndex("g")
    value._db().executemany(
        "INSERT INTO nodes(id,game_id,directory_name) VALUES(?,?,?)", [("a", "g", "A"), ("b", "g", "B")]
    )
    value._db().commit()
    yield value
    value.close()


def mod(index, identity="1" * 32, node="a", folder="Box"):
    base = index.paths.node_path(node) if node else index.paths.roots["g"]
    return Mod(
        mod_id=identity,
        game_id="g",
        node_id=node,
        folder_name=folder,
        name=folder,
        path=str(base / folder / "body.zip"),
        imported_at="2026-01-02T03:04:05",
    )


def test_same_name_different_nodes_and_root_collision(index):
    one, two = mod(index), mod(index, "2" * 32, "b")
    assert index.add_mod(one) and index.add_mod(two)
    assert index.find_mod_id_by_path(one.path, "g") == one.mod_id
    assert index.find_mod_id_by_path(two.path, "g") == two.mod_id
    assert index.add_mod(mod(index, "3" * 32, None))
    assert not index.add_mod(mod(index, "4" * 32, None, "box"))
    assert index.get_mod_count() == 3
    assert {r[0] for r in index._db().execute("SELECT path FROM mods")} == {"body.zip"}


def test_join_status_and_preview_survive_node_move_without_mod_writes(index):
    item = mod(index)
    container = index.paths.node_path("a") / "Box"
    item.preview_images = [
        str(container / "preview.png"),
        str(index.paths.preview_roots["g"] / item.mod_id / "cache.png"),
    ]
    assert index.add_mod(item)
    index.upsert_deployment(DeploymentRecord(item.mod_id, DeploymentState.ACTIVE, "D:/deployed/Box"))
    statements = []
    index._db().set_trace_callback(statements.append)
    loaded = index.get_all_mods("g")
    assert loaded[0].is_enabled and loaded[0].deployed_path == "D:/deployed/Box"
    assert sum("LEFT JOIN deployment_records" in s for s in statements) == 1
    assert not any("FROM deployment_records WHERE" in s for s in statements)
    index.paths.move_node("a", "b", "Moved")
    changed = index.get_mod(item.mod_id)
    assert changed.path == str(index.paths.roots["g"] / "B/Moved/Box/body.zip")
    assert changed.preview_images[0] == str(index.paths.roots["g"] / "B/Moved/Box/preview.png")
    assert changed.preview_images[1] == item.preview_images[1]
    assert changed.imported_at == item.imported_at
    assert not any(s.startswith("UPDATE mods") for s in statements)
    stored = index._db().execute("SELECT preview_images_json,info_json FROM mods").fetchone()
    assert json.loads(stored[0])[0] == {"scope": "container", "path": "preview.png"}
    assert "name" not in json.loads(stored[1])


def test_cleanup_failure_retains_source_join_and_unified_identity(index):
    item = mod(index)
    assert index.add_mod(item)
    index.upsert_deployment(DeploymentRecord(item.mod_id, DeploymentState.ERROR, "D:/locked/Box"))
    # 物理清理失败时不调用索引删除。
    record = index.get_deployments("g")[0]
    assert record.mod_id == item.mod_id and record.actual_path == "D:/locked/Box"
    assert index.get_mod_game_id(record.mod_id) == "g"
    assert index.get_mod_ids() == {item.mod_id}
    assert index.delete_mod(item.mod_id)
    assert index.get_mod(item.mod_id) is None
    assert not index.get_deployments("g")


def test_commit_false_is_really_rollbackable(index):
    item = mod(index)
    assert index.add_mod(item, commit=False)
    assert index.in_transaction
    index._db().rollback()
    assert index.get_mod(item.mod_id) is None


def test_new_marker_identity_is_not_replaced_by_path_identity(index):
    first = mod(index)
    assert index.add_mod(first)
    second = mod(index, "2" * 32, "a", "New")
    container = index.paths.node_path("a") / "New"
    container.mkdir(parents=True)
    (container / ".d3dxmod").write_text(json.dumps({"mod_id": second.mod_id, "body": "body.zip"}))
    assert index.add_mod(second)
    assert index.get_mod_ids() == {first.mod_id, second.mod_id}


def test_local_get_does_not_read_unrelated_rows(index):
    item = mod(index)
    assert index.add_mod(item)
    work = []
    for size in (500, 5000):
        index._db().executemany(
            "INSERT OR IGNORE INTO mods(mod_id,game_id,folder_name,path,imported_at) VALUES(?,?,?,?,?)",
            [(f"unrelated-{i}", "g", f"unrelated-{i}", "body.zip", "2026") for i in range(size)],
        )
        index._db().commit()
        steps = [0]

        def progress():
            steps[0] += 1
            return 0

        index._db().set_progress_handler(progress, 1)
        assert index.get_mod(item.mod_id).path == item.path
        index._db().set_progress_handler(None, 0)
        work.append(steps[0])
    # SQLite 首条预编译语句可能执行两个初始化指令；无关记录增多不应增加遍历开销。
    assert work[1] <= work[0] + 2 and max(work) < 80


def test_incompatible_schema_is_preserved_without_opening(temp_data_dir):
    db = temp_data_dir / "data/mod_index.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE mods(id TEXT PRIMARY KEY,path TEXT)")
    conn.execute("INSERT INTO mods VALUES('old','unchanged')")
    conn.commit()
    conn.close()
    old = ModIndex("g")
    assert old._conn is None and "不符合当前格式" in old.schema_error
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT * FROM mods").fetchall() == [("old", "unchanged")]
    conn.close()
