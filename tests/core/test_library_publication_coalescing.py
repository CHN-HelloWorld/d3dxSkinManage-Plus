import pytest

from src.core.watcher import FileChange


def change(kind, batch, path="container/.d3dxmod", *, root="root", generation=1, root_identity=(1, 2)):
    return FileChange(root, generation, kind, path, batch_id=batch, root_identity=root_identity)


def test_repeated_modified_keeps_last_and_all_native_batch_barriers():
    from src.core.library_publication import _coalesce_source_modifications

    rows = [
        change("modified", 1),
        change("batch_end", 1, "root"),
        change("modified", 2),
        change("batch_end", 2, "root"),
    ]
    assert _coalesce_source_modifications(rows) == rows[1:]


def test_source_publication_applies_one_snapshot_and_retains_completed_batches(monkeypatch):
    from types import SimpleNamespace

    from src.core import library_publication as publication
    from src.core.library_events import LocalChanges

    rows = [
        change("modified", 1),
        change("batch_end", 1, "root"),
        change("modified", 2),
        change("batch_end", 2, "root"),
    ]
    applied = []

    def handle(event):
        applied.append(event)
        return LocalChanges()

    monkeypatch.setattr(publication, "_ready_events", lambda manager, events, channel: rows)
    monkeypatch.setattr(publication, "_service", lambda manager, generation: SimpleNamespace(handle=handle))
    publication.source_events(SimpleNamespace(), rows)
    assert applied == rows[1:]


@pytest.mark.parametrize(
    "kind",
    [
        "moved",
        "created",
        "deleted",
        "overflow",
        "zero_bytes",
        "root_replaced",
        "watch_failed",
        "root_offline",
        "rename_unpaired",
    ],
)
def test_semantic_barriers_preserve_modifications_on_both_sides(kind):
    from src.core.library_publication import _coalesce_source_modifications

    rows = [change("modified", 1), change(kind, 1, "other"), change("modified", 2), change("batch_end", 2, "root")]
    assert _coalesce_source_modifications(rows) == rows


@pytest.mark.parametrize(
    "fields", [dict(root="other-root"), dict(generation=2), dict(root_identity=(1, 3)), dict(path="other/.d3dxmod")]
)
def test_identity_and_target_scopes_never_coalesce(fields):
    from src.core.library_publication import _coalesce_source_modifications

    rows = [change("modified", 1), change("modified", 2, **fields), change("batch_end", 2, "root")]
    assert _coalesce_source_modifications(rows) == rows
