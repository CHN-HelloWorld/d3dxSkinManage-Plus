from __future__ import annotations

import json
import sqlite3

import pytest

from src.core.mod_index import ModIndex
from src.models.mod import Mod, ModInfo


@pytest.fixture
def index(temp_data_dir):
    from tests.helpers import register_games

    register_games("game-a", "game-b")
    idx = ModIndex("game-a")
    yield idx
    idx.close()


def make_mod(index, mod_id, **kwargs):
    kwargs.pop("path", None)
    kwargs.setdefault("folder_name", mod_id)
    kwargs.setdefault("name", mod_id)
    return Mod(
        mod_id=mod_id,
        game_id=index.game_id,
        path=str(index.paths.roots[index.game_id] / kwargs["folder_name"] / "body.zip"),
        **kwargs,
    )


def add_mod(index, mod_id, tags=()):
    mod = make_mod(
        index,
        mod_id=mod_id,
        name=mod_id,
        folder_name=mod_id,
        path=f"library/{mod_id}",
        info=ModInfo(tags=list(tags), author="original", version="1"),
    )
    assert index.add_mod(mod)
    return index.get_mod(mod_id)


def service_for(index):
    from src.core.tag_service import TagService

    return TagService(index)


def test_normalization_and_stable_palette_colors():
    from src.models.tag import default_tag_color, normalize_tag_name, tag_key

    assert normalize_tag_name("  ＨＤ   Texture  ") == "HD Texture"
    assert tag_key("  ＨＤ   Texture  ") == "hd texture"
    for name, expected in [("a", "blue"), ("A", "blue"), ("test", "mint"), ("高清材质", "purple"), ("可切换", "amber")]:
        assert default_tag_color(name) == expected


@pytest.mark.parametrize("name", ["", " ", "x" * 25, "a,b", "a，b", "a\nb", "a\x00b", "a\x7fb"])
def test_invalid_new_names_are_rejected(name):
    from src.models.tag import validate_tag_name

    with pytest.raises(ValueError):
        validate_tag_name(name)


def test_unicode_length_counts_codepoints():
    from src.models.tag import validate_tag_name

    assert validate_tag_name("😀" * 24) == "😀" * 24


@pytest.mark.parametrize("dark", [False, True])
@pytest.mark.parametrize(
    "color",
    ["blue", "mint", "purple", "amber", "#000000", "#FFFFFF", "#FF0000", "#00FF00", "#0000FF", "#FFFF00", "#808080"],
)
def test_chip_colors_have_readable_contrast(color, dark):
    from src.models.tag import tag_colors

    bg, fg = tag_colors(color, dark=dark)

    def luminance(value):
        rgb = [int(value[i : i + 2], 16) / 255 for i in (1, 3, 5)]
        linear = [v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4 for v in rgb]
        return sum(v * weight for v, weight in zip(linear, (0.2126, 0.7152, 0.0722)))

    a, b = sorted((luminance(bg), luminance(fg)))
    assert (b + 0.05) / (a + 0.05) >= 4.5


def test_index_deduplicates_associations_and_preserves_first_display_name(index):
    mod = add_mod(index, "one", ["HD", "hd", " ＨＤ ", "服装"])
    tags = service_for(index).list_tags()
    assert {tag.name: tag.usage_count for tag in tags} == {"HD": 1, "服装": 1}
    assert mod.info.tags == ["HD", "服装"]
    assert [
        row[0] for row in index._db().execute("SELECT position FROM mod_tags WHERE mod_id='one' ORDER BY position")
    ] == [0, 1]


def test_draft_changes_never_write_until_commit(index):
    mod = add_mod(index, "one", ["old"])
    service = service_for(index)
    draft = service.begin_edit(mod)
    draft.rename(draft.selected_ids[0], "renamed")
    draft.add_names(["new"])
    assert draft.is_dirty
    assert index.get_mod("one").info.tags == ["old"]
    assert [tag.name for tag in service.list_tags()] == ["old"]
    assert mod.info.tags == ["old"]


def test_shared_rename_keeps_id_and_updates_all_mirrors(index):
    first = add_mod(index, "one", ["old", "other"])
    add_mod(index, "two", ["old"])
    service = service_for(index)
    draft = service.begin_edit(first)
    tag_id = draft.selected_ids[0]
    draft.rename(tag_id, "renamed")
    draft.set_color(tag_id, "#123ABC")
    result = service.commit_edit(draft, {"author": "edited"})
    assert {m.mod_id for m in result.mods} == {"one", "two"}
    assert index.get_mod("one").info.tags == ["renamed", "other"]
    assert index.get_mod("two").info.tags == ["renamed"]
    assert index.get_mod("one").info.author == "edited"
    shared = next(t for t in service.list_tags() if t.id == tag_id)
    assert (shared.name, shared.color, shared.usage_count) == ("renamed", "#123ABC", 2)
    assert first.info.tags == ["old", "other"]


def test_saved_candidate_survives_rebuild_with_color_and_identity(index):
    mod = add_mod(index, "one", ["old"])
    service = service_for(index)
    draft = service.begin_edit(mod)
    tag_id = draft.selected_ids[0]
    draft.set_color(tag_id, "#123ABC")
    service.commit_edit(draft)
    assert index.get_mod("one").info.tags == ["old"]
    assert [tag.id for tag in service.begin_edit(mod).candidates()] == [tag_id]
    assert service.list_tags(used_only=True)[0].id == tag_id
    assert index.update_mod(mod)
    assert index.clear_mods_for_game("game-a") == 1
    assert index.add_mod(mod)
    tag = service.list_tags()[0]
    assert (tag.id, tag.color) == (tag_id, "#123ABC")


@pytest.mark.parametrize("save_before_recreate", [False, True])
def test_removing_then_entering_same_name_creates_a_new_tag(index, save_before_recreate):
    mod = add_mod(index, "one", ["Shared"])
    add_mod(index, "two", ["Shared"])
    service = service_for(index)
    draft = service.begin_edit(mod)
    old_id = draft.selected_ids[0]
    draft.set_color(old_id, "#123ABC")
    service.commit_edit(draft)
    draft = service.begin_edit(index.get_mod("one"))
    draft.remove_candidate(old_id)
    if save_before_recreate:
        service.commit_edit(draft)
        draft = service.begin_edit(index.get_mod("one"))

    new_id = draft.add_names(["Shared"])[0]

    assert new_id != old_id
    assert draft.definitions[new_id].color == "blue"
    service.commit_edit(draft)
    assert [(tag.id, tag.name, tag.usage_count) for tag in service.list_tags()] == [(new_id, "Shared", 1)]
    assert index.get_mod("one").info.tags == ["Shared"]
    assert index.get_mod("two").info.tags == []


def test_global_delete_only_commits_in_its_game(index):
    first = add_mod(index, "one", ["shared", "keep"])
    add_mod(index, "two", ["shared"])
    index.game_id = "game-b"
    add_mod(index, "foreign", ["shared"])
    index.game_id = "game-a"
    service = service_for(index)
    draft = service.begin_edit(first)
    tag_id = draft.selected_ids[0]
    draft.remove_candidate(tag_id)
    assert draft.usage(tag_id) == 0
    assert index.get_mod("two").info.tags == ["shared"]
    result = service.commit_edit(draft)
    assert result.deleted_tag_ids == {tag_id}
    assert index.get_mod("one").info.tags == ["keep"]
    assert index.get_mod("two").info.tags == []
    assert index.get_mod("foreign").info.tags == ["shared"]


def test_new_candidate_remains_after_detaching_current_mod(index):
    mod = add_mod(index, "one")
    service = service_for(index)
    draft = service.begin_edit(mod)
    new_id = draft.add_names(["candidate"])[0]
    draft.detach(new_id)
    service.commit_edit(draft)
    assert index.get_mod("one").info.tags == []
    tag = service.list_tags()[0]
    assert (tag.name, tag.usage_count) == ("candidate", 0)
    assert tag.last_used_at == ""


def test_batch_validation_is_atomic_and_legacy_limits_are_preserved(index):
    legacy = ["old-" + str(i) for i in range(21)] + ["超长" * 20]
    mod = add_mod(index, "one", legacy)
    service = service_for(index)
    draft = service.begin_edit(mod)
    with pytest.raises(ValueError):
        draft.add_names(["valid", "x" * 25])
    assert not draft.is_dirty
    service.commit_edit(draft, {"author": "other"})
    assert index.get_mod("one").info.tags == legacy


def test_valid_batch_deduplicates_before_limit_check(index):
    service = service_for(index)
    draft = service.begin_edit(add_mod(index, "one"))
    draft.add_names(["HD", "hd", " ＨＤ "])
    assert len(draft.selected_ids) == 1
    with pytest.raises(ValueError):
        draft.add_names([f"tag-{i}" for i in range(20)])
    assert len(draft.selected_ids) == 1
    assert len(draft.definitions) == 1


def test_usage_counts_adjust_for_current_draft(index):
    first = add_mod(index, "one", ["shared"])
    second = add_mod(index, "two")
    service = service_for(index)
    tag_id = service.list_tags()[0].id
    draft = service.begin_edit(second)
    assert draft.usage(tag_id) == 1
    draft.attach(tag_id)
    assert draft.usage(tag_id) == 2
    draft.detach(tag_id)
    assert draft.usage(tag_id) == 1
    assert service.begin_edit(first).usage(tag_id) == 1


def test_metadata_and_color_changes_do_not_advance_recency(index):
    mod = add_mod(index, "one", ["old"])
    service = service_for(index)
    before = service.list_tags()[0].last_used_at
    draft = service.begin_edit(mod)
    draft.set_color(draft.selected_ids[0], "amber")
    service.commit_edit(draft, {"description": "metadata only"})
    assert service.list_tags()[0].last_used_at == before
    assert index.update_mod(index.get_mod("one"))
    assert service.list_tags()[0].last_used_at == before


def test_concurrent_rename_and_color_edit_preserve_both_deltas(index):
    mod = add_mod(index, "one", ["old"])
    service = service_for(index)
    rename = service.begin_edit(mod)
    recolor = service.begin_edit(mod)
    tag_id = rename.selected_ids[0]
    rename.rename(tag_id, "new")
    service.commit_edit(rename)
    recolor.set_color(tag_id, "amber")
    service.commit_edit(recolor)
    tag = service.list_tags()[0]
    assert (tag.name, tag.color) == ("new", "amber")
    assert index.get_mod("one").info.tags == ["new"]


def test_concurrent_unrelated_metadata_and_unknown_json_keys_are_preserved(index):
    mod = add_mod(index, "one", ["old"])
    service = service_for(index)
    draft = service.begin_edit(mod)
    raw = json.loads(index._db().execute("SELECT info_json FROM mods WHERE mod_id='one'").fetchone()[0])
    raw.update(version="concurrent", future_field={"preserve": True})
    index._db().execute("UPDATE mods SET info_json=? WHERE mod_id='one'", (json.dumps(raw),))
    index._db().commit()
    service.commit_edit(draft, {"author": "new", "version": "1"})
    saved = json.loads(index._db().execute("SELECT info_json FROM mods WHERE mod_id='one'").fetchone()[0])
    assert saved["version"] == "concurrent"
    assert saved["future_field"] == {"preserve": True}
    assert saved["author"] == "new"


def test_concurrent_name_collision_rejects_entire_edit(index):
    mod = add_mod(index, "one", ["old"])
    service = service_for(index)
    draft = service.begin_edit(mod)
    draft.rename(draft.selected_ids[0], "occupied")
    add_mod(index, "two", ["OCCUPIED"])
    with pytest.raises(ValueError):
        service.commit_edit(draft, {"author": "must not save"})
    assert index.get_mod("one").info.author == "original"
    assert index.get_mod("one").info.tags == ["old"]
    assert draft.is_dirty


def test_sql_failure_rolls_back_shared_names_metadata_and_relations(index):
    mod = add_mod(index, "one", ["old"])
    add_mod(index, "two", ["old"])
    service = service_for(index)
    draft = service.begin_edit(mod)
    draft.rename(draft.selected_ids[0], "new")
    draft.add_names(["temporary"])
    index._db().execute("""CREATE TRIGGER fail_tag_save BEFORE UPDATE OF info_json ON mods
        WHEN NEW.mod_id = 'two' BEGIN SELECT RAISE(ABORT, 'simulated disk failure'); END""")
    index._db().commit()
    with pytest.raises(sqlite3.Error, match="simulated disk failure"):
        service.commit_edit(draft, {"author": "must not save"})
    assert index.get_mod("one").info.tags == ["old"]
    assert index.get_mod("one").info.author == "original"
    assert index.get_mod("two").info.tags == ["old"]
    assert [t.name for t in service.list_tags()] == ["old"]
    assert mod.info.tags == ["old"]


def test_stale_draft_cannot_write_after_game_switch(index):
    mod = add_mod(index, "one", ["old"])
    service = service_for(index)
    draft = service.begin_edit(mod)
    draft.add_names(["new"])
    index.game_id = "game-b"
    with pytest.raises(ValueError):
        service.commit_edit(draft)
    assert index.get_mod("one").info.tags == ["old"]


def test_relations_participate_in_outer_import_transaction(index):
    with pytest.raises(RuntimeError, match="abort batch"):
        with index.transaction():
            assert index.add_mod(
                make_mod(index, mod_id="one", path="library/one", info=ModInfo(tags=["new"])), commit=False
            )
            raise RuntimeError("abort batch")
    assert index.get_mod("one") is None
    assert service_for(index).list_tags() == []


def test_relation_failure_cannot_leave_partial_mod_row(index):
    index._db().execute("""CREATE TRIGGER fail_relation BEFORE INSERT ON mod_tags
        BEGIN SELECT RAISE(ABORT, 'relation failure'); END""")
    index._db().commit()
    assert not index.add_mod(make_mod(index, mod_id="one", path="library/one", info=ModInfo(tags=["new"])))
    assert index.get_mod("one") is None
    assert service_for(index).list_tags() == []


def test_reopen_does_not_restore_deleted_definitions(index):
    add_mod(index, "one", ["legacy"])
    index.close()
    reopened = ModIndex("game-a")
    try:
        service = service_for(reopened)
        assert [(t.name, t.usage_count) for t in service.list_tags()] == [("legacy", 1)]
        draft = service.begin_edit(reopened.get_mod("one"))
        draft.remove_candidate(draft.selected_ids[0])
        service.commit_edit(draft)
    finally:
        reopened.close()
    reopened = ModIndex("game-a")
    try:
        assert service_for(reopened).list_tags() == []
        assert reopened.get_mod("one").info.tags == []
    finally:
        reopened.close()


@pytest.mark.parametrize("old_version_tags", [["Other"], [], ["Created by older version"]])
def test_explicit_metadata_restore_preserves_colors_on_reopen(index, old_version_tags):
    add_mod(index, "one", ["Shared"])
    second = add_mod(index, "two", ["Shared", "Other"])
    service = service_for(index)
    draft = service.begin_edit(second)
    other_id = next(tag.id for tag in draft.definitions.values() if tag.name == "Other")
    draft.set_color(other_id, "#123ABC")
    service.commit_edit(draft)
    # 元数据恢复在所属事务中更新两份映射。
    info = json.loads(index._db().execute("SELECT info_json FROM mods WHERE mod_id='one'").fetchone()[0])
    info["tags"] = old_version_tags
    info["future_info"] = {"keep": True}
    index._db().execute("UPDATE mods SET info_json=? WHERE mod_id='one'", (json.dumps(info),))
    from src.core.tag_store import sync_mod_tags

    sync_mod_tags(index._db(), "one", "game-a", old_version_tags, "2026-01-01")
    index._db().commit()
    index.close()

    reopened = ModIndex("game-a")
    try:
        service = service_for(reopened)
        draft = service.begin_edit(reopened.get_mod("one"))
        assert draft.names == old_version_tags
        other = next(tag for tag in service.list_tags() if tag.id == other_id)
        assert other.color == "#123ABC"
        assert other.usage_count == 1 + ("Other" in old_version_tags)
        service.commit_edit(draft, {"author": "saved with new version"})
        saved = json.loads(reopened._db().execute("SELECT info_json FROM mods WHERE mod_id='one'").fetchone()[0])
        assert saved["tags"] == old_version_tags
        assert saved["future_info"] == {"keep": True}
        assert reopened.get_mod("two").info.tags == ["Shared", "Other"]
    finally:
        reopened.close()


def test_delete_and_purge_cleanup_only_their_game(index):
    add_mod(index, "one", ["one-tag"])
    index.game_id = "game-b"
    add_mod(index, "two", ["two-tag"])
    index.game_id = "game-a"
    assert index.delete_mod("one")
    assert service_for(index).list_tags()[0].usage_count == 0
    index.purge_game("game-a")
    assert service_for(index).list_tags() == []
    index.game_id = "game-b"
    assert service_for(index).list_tags()[0].usage_count == 1


def test_null_legacy_tags_do_not_leave_an_open_write_transaction(index):
    mod = make_mod(index, mod_id="null-tags", path="library/null-tags", info=ModInfo(tags=None))
    assert index.add_mod(mod)
    assert index.get_mod(mod.mod_id).info.tags == []
    assert not index._db().in_transaction


def test_serialization_failure_rolls_back_savepoint_before_next_write(index):
    bad = make_mod(index, mod_id="bad-json", path="library/bad", info=ModInfo(custom_data={"invalid": object()}))
    assert not index.add_mod(bad)
    assert not index._db().in_transaction
    add_mod(index, "next", ["good"])
    assert index.get_mod("bad-json") is None


def test_old_import_cannot_move_shared_tag_recency_backwards(index):
    mod = add_mod(index, "one", ["shared"])
    tag = service_for(index).list_tags()[0]
    older = make_mod(
        index, mod_id="old-import", path="library/old", imported_at="2000-01-01T00:00:00", info=ModInfo(tags=["shared"])
    )
    assert index.add_mod(older)
    assert service_for(index).list_tags()[0].last_used_at >= tag.last_used_at
    assert index.get_mod(mod.mod_id).info.tags == ["shared"]


def test_color_only_changes_refresh_references_without_sidecar_work(index):
    mod = add_mod(index, "one", ["shared"])
    add_mod(index, "two", ["shared"])
    service = service_for(index)
    draft = service.begin_edit(mod)
    draft.set_color(draft.selected_ids[0], "amber")
    result = service.commit_edit(draft)
    assert {mod.mod_id for mod in result.mods} == {"one"}
    assert result.affected_mod_ids == {"one", "two"}
    assert result.sidecar_mod_ids == set()
    draft = service.begin_edit(index.get_mod("one"))
    draft.rename(draft.selected_ids[0], "renamed")
    assert service.commit_edit(draft).sidecar_mod_ids == {"one", "two"}


def test_color_only_preflight_targets_edited_mod_without_all_references(index):
    mod = add_mod(index, "one", ["shared"])
    add_mod(index, "two", ["shared"])
    service = service_for(index)
    draft = service.begin_edit(mod)
    draft.set_color(draft.selected_ids[0], "amber")
    targets = []

    result = service.commit_edit(draft, before_write=lambda ids: targets.append(set(ids)))

    assert targets == [{"one"}]
    assert {mod.mod_id for mod in result.mods} == {"one"}
    assert result.affected_mod_ids == {"one", "two"}


def test_shared_rename_reads_rows_and_paths_in_batches(index):
    mods = [add_mod(index, f"mod-{number:03d}", ["shared", "other"]) for number in range(48)]
    service = service_for(index)
    draft = service.begin_edit(mods[0])
    draft.rename(draft.selected_ids[0], "renamed")
    queries = []
    index._db().set_trace_callback(queries.append)
    try:
        result = service.commit_edit(draft)
    finally:
        index._db().set_trace_callback(None)

    selects = [query for query in queries if query.lstrip().upper().startswith("SELECT")]
    assert len(selects) < 20, "Shared edits must not re-query and hydrate each Mod separately"
    assert len(result.mods) == 48
    assert all(mod.info.tags == ["renamed", "other"] for mod in result.mods)
    assert result.sidecar_mod_ids == {mod.mod_id for mod in mods}


def test_file_callbacks_run_before_any_tag_sql_is_dirty(index):
    mod = add_mod(index, "one", ["shared"])
    service = service_for(index)
    draft = service.begin_edit(mod)
    draft.rename(draft.selected_ids[0], "renamed")
    observed = []

    def before_write(ids):
        observed.append((index.in_transaction, index.get_mod("one").info.tags))

    def write_sidecars(result):
        observed.append((index.in_transaction, index.get_mod("one").info.tags))
        assert result.mods[0].info.tags == ["renamed"]

    result = service.commit_edit(draft, before_write=before_write, write_sidecars=write_sidecars)

    assert observed == [(False, ["shared"]), (False, ["shared"])]
    assert index.get_mod("one").info.tags == result.mods[0].info.tags == ["renamed"]


def test_new_shared_reference_during_preflight_rejects_stale_save(index):
    mod = add_mod(index, "one", ["shared"])
    service = service_for(index)
    draft = service.begin_edit(mod)
    draft.rename(draft.selected_ids[0], "renamed")

    with pytest.raises(ValueError):
        service.commit_edit(draft, before_write=lambda ids: add_mod(index, "late", ["shared"]))

    assert [tag.name for tag in service.list_tags()] == ["shared"]
    assert index.get_mod("one").info.tags == ["shared"]
    assert index.get_mod("late").info.tags == ["shared"]
    assert not index.in_transaction


def test_name_collision_during_file_write_keeps_other_committed_tag(index):
    mod = add_mod(index, "one", ["shared"])
    service = service_for(index)
    draft = service.begin_edit(mod)
    original_id = draft.selected_ids[0]
    draft.rename(original_id, "renamed")

    with pytest.raises(ValueError):
        service.commit_edit(draft, write_sidecars=lambda result: add_mod(index, "late", ["renamed"]))

    tags = {tag.name: tag.id for tag in service.list_tags()}
    assert tags["shared"] == original_id
    assert tags["renamed"] != original_id
    assert index.get_mod("one").info.tags == ["shared"]
    assert index.get_mod("late").info.tags == ["renamed"]
    assert not index.in_transaction
