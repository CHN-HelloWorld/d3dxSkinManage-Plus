from pathlib import Path

import pytest


def test_staging_is_outside_candidate_root_and_only_published_at_commit(tmp_path):
    from src.core.preview_cache_index import (
        build_preview_cache_index,
        preview_cache_staging,
        publish_preview_cache,
    )

    cache = tmp_path / "game" / "preview_cache" / ("a" * 24)
    with preview_cache_staging(cache) as stage:
        image = stage / "preview.png"
        image.write_bytes(b"complete")
        assert not stage.is_relative_to(cache.parent)
        assert build_preview_cache_index(cache.parent) == {}
        assert not cache.exists()
        final = publish_preview_cache(stage, cache, [image], content_hash="b" * 40)
        assert final == [cache / "preview.png"]
        assert final[0].read_bytes() == b"complete"
        assert build_preview_cache_index(cache.parent)["b" * 40] == (cache,)
    assert not stage.exists()
    assert sorted(p for p in cache.iterdir() if p.suffix == ".png") == final


def test_unpublished_failure_and_abandoned_stage_are_never_candidates(tmp_path):
    from src.core.preview_cache_index import build_preview_cache_index, preview_cache_staging

    cache = tmp_path / "game" / "preview_cache" / ("a" * 24)
    with pytest.raises(OSError, match="disk failure"):
        with preview_cache_staging(cache) as stage:
            (stage / "preview.png").write_bytes(b"partial")
            raise OSError("disk failure")
    assert not stage.exists()
    assert not cache.exists()
    # 进程中断可能在根目录外留下暂存目录。
    abandoned = cache.parent.parent / ".preview-abandoned" / "ready"
    abandoned.mkdir(parents=True)
    (abandoned / "preview.png").write_bytes(b"partial")
    assert build_preview_cache_index(cache.parent) == {}


def test_failed_publication_restores_previous_complete_directory(tmp_path, monkeypatch):
    import os

    from src.core.preview_cache_index import preview_cache_staging, publish_preview_cache

    cache = tmp_path / "game" / "preview_cache" / ("a" * 24)
    cache.mkdir(parents=True)
    (cache / "preview.png").write_bytes(b"previous")
    replace = os.replace
    with preview_cache_staging(cache) as stage:
        (stage / "preview.png").write_bytes(b"next")

        def fail_publish(source, target):
            if Path(source).name == stage.name:
                raise OSError("publish failed")
            return replace(source, target)

        monkeypatch.setattr(os, "replace", fail_publish)
        with pytest.raises(OSError, match="publish failed"):
            publish_preview_cache(stage, cache, [stage / "preview.png"])
        assert (cache / "preview.png").read_bytes() == b"previous"
    assert not stage.exists()


def test_missing_staged_file_does_not_replace_existing_cache(tmp_path):
    from src.core.preview_cache_index import preview_cache_staging, publish_preview_cache

    cache = tmp_path / "game" / "preview_cache" / ("a" * 24)
    cache.mkdir(parents=True)
    (cache / "preview.png").write_bytes(b"previous")
    with preview_cache_staging(cache) as stage:
        with pytest.raises(OSError):
            publish_preview_cache(stage, cache, [stage / "missing.png"])
        assert (cache / "preview.png").read_bytes() == b"previous"
