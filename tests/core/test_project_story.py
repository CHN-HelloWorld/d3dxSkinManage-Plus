import json
import sys
from argparse import Namespace
from fnmatch import fnmatch
from pathlib import Path
from types import SimpleNamespace

import pytest


def test_story_reloads_replaced_manifest_and_media(tmp_path):
    from src.utils.project_story import load_project_story

    manifest = tmp_path / "story.json"
    manifest.write_text(json.dumps({"title": "First", "video": "first.mp4", "cover": "first.png"}))
    (tmp_path / "first.mp4").write_bytes(b"first film")
    first = load_project_story(tmp_path)
    assert first.title == "First"
    assert first.video_path.read_bytes() == b"first film"
    assert first.cover_path == tmp_path / "first.png"

    manifest.write_text(json.dumps({"title": "Final", "video": "final.mp4", "cover": "final.png"}))
    (tmp_path / "final.mp4").write_bytes(b"replacement film")
    final = load_project_story(tmp_path)
    assert final.title == "Final"
    assert final.video_path.read_bytes() == b"replacement film"
    assert final.cover_path == tmp_path / "final.png"


@pytest.mark.parametrize("contents", ["{", "[]", '{"video": null, "cover": 42, "title": []}'])
def test_broken_manifest_keeps_default_media_usable(tmp_path, contents):
    from src.utils.project_story import load_project_story

    (tmp_path / "story.json").write_text(contents)
    (tmp_path / "film.mp4").write_bytes(b"usable film")
    story = load_project_story(tmp_path)
    assert story.video_path.read_bytes() == b"usable film"
    assert story.cover_path == tmp_path / "cover.png"
    assert story.title


@pytest.mark.parametrize("filename", ["../outside.mp4", "https://example.com/film.mp4", "C:/outside.mp4"])
def test_media_paths_stay_inside_the_replaceable_resource_directory(tmp_path, filename):
    from src.utils.project_story import load_project_story

    (tmp_path / "story.json").write_text(json.dumps({"video": filename}))
    assert load_project_story(tmp_path).video_path == tmp_path / "film.mp4"


def test_onefile_loads_media_next_to_launcher_not_temporary_unpack_or_data_dir(tmp_path, monkeypatch):
    from src.utils import paths
    from src.utils.project_story import load_project_story

    installed = tmp_path / "installed"
    media = installed / "resources" / "about" / "story"
    media.mkdir(parents=True)
    (media / "film.mp4").write_bytes(b"external replacement")
    monkeypatch.setattr(
        paths, "__compiled__", SimpleNamespace(onefile=True, containing_dir=str(installed)), raising=False
    )
    monkeypatch.setattr(sys, "executable", str(tmp_path / "unpacked" / "app.exe"))
    monkeypatch.setattr(paths, "get_app_dir", lambda: tmp_path / "fallback-appdata")
    assert load_project_story().video_path.read_bytes() == b"external replacement"


def test_standalone_media_location_does_not_depend_on_launch_cwd(tmp_path, monkeypatch):
    from src.utils import paths
    from src.utils.project_story import load_project_story

    installed = tmp_path / "installed"
    monkeypatch.setattr(paths, "__compiled__", SimpleNamespace(onefile=False), raising=False)
    monkeypatch.setattr(sys, "executable", str(installed / "app.exe"))
    monkeypatch.chdir(tmp_path)
    assert load_project_story().video_path == installed / "resources/about/story/film.mp4"


def test_build_stages_only_the_selected_story_assets_and_includes_them_in_zip(tmp_path, monkeypatch):
    import zipfile

    from scripts.release import build

    source = tmp_path / "source"
    source.mkdir()
    (source / "story.json").write_text(json.dumps({"video": "release.mp4", "cover": "release.png"}))
    (source / "release.mp4").write_bytes(b"selected film")
    (source / "release.png").write_bytes(b"selected cover")
    (source / "unused.mp4").write_bytes(b"old draft")
    (source / "README.md").write_text("Replacement instructions")
    monkeypatch.setattr(build, "PROJECT_STORY", source)
    monkeypatch.setattr(build, "PROJECT_BUILD", tmp_path)
    product = tmp_path / "product"
    media = build.stage_project_story(product)
    assert (media / "release.mp4").read_bytes() == b"selected film"
    assert (media / "release.png").read_bytes() == b"selected cover"
    assert not (media / "unused.mp4").exists()
    archive = build.make_zip([(media, "resources/about/story")])
    with zipfile.ZipFile(archive) as stream:
        names = {str(Path(name).relative_to(Path(name).parts[0])).replace("\\", "/") for name in stream.namelist()}
        assert names == {
            "resources/about/story/story.json",
            "resources/about/story/release.mp4",
            "resources/about/story/release.png",
            "resources/about/story/README.md",
        }


@pytest.mark.parametrize("onefile", [False, True])
def test_story_is_excluded_from_embedded_resources_in_both_build_modes(onefile):
    from scripts.release import build

    command = build.build_command(Namespace(onefile=onefile, console=False, nuitka_args=[]))
    exclusions = [option.split("=", 1)[1] for option in command if option.startswith("--noinclude-data-files=")]
    assert any(fnmatch("resources/about/story/film.mp4", pattern) for pattern in exclusions)
    assert not any(fnmatch("resources/about/avatars/aether.png", pattern) for pattern in exclusions)


def test_rebuilding_story_drops_previous_drafts_from_the_product(tmp_path, monkeypatch):
    from scripts.release import build

    source = tmp_path / "source"
    source.mkdir()
    (source / "film.mp4").write_bytes(b"new film")
    (source / "cover.png").write_bytes(b"new cover")
    monkeypatch.setattr(build, "PROJECT_STORY", source)
    directory = tmp_path / "product/resources/about/story"
    directory.mkdir(parents=True)
    (directory / "old-draft.mp4").write_bytes(b"old film")
    build.stage_project_story(tmp_path / "product")
    assert {path.name for path in directory.iterdir()} == {"film.mp4", "cover.png"}


def test_story_manifest_accepts_utf8_bom_and_defaults_invalid_text_fields(tmp_path):
    from src.utils.project_story import load_project_story

    (tmp_path / "story.json").write_text(
        json.dumps({"title": "新片", "video": "new.mp4", "subtitle": 42, "edition": []}),
        encoding="utf-8-sig",
    )
    story = load_project_story(tmp_path)
    assert story.title == "新片"
    assert story.video_path == tmp_path / "new.mp4"
    assert isinstance(story.subtitle, str) and story.subtitle
    assert isinstance(story.edition, str)


def test_clean_removes_onefile_story_and_manifest_but_preserves_other_resources(tmp_path, monkeypatch):
    from scripts.release import build

    product = tmp_path / "build"
    story = product / "resources/about/story"
    story.mkdir(parents=True)
    (story / "film.mp4").write_bytes(b"generated film")
    retained = product / "resources/keep.txt"
    retained.write_text("unrelated resource")
    manifest = build.generate_release_manifest([(story, "resources/about/story")], product)
    monkeypatch.setattr(build, "PROJECT_BUILD", product)
    monkeypatch.setattr(build, "STAGING_ROOT", product)
    monkeypatch.setattr(build, "STAGING_DIR", product / "nuitka")
    assert build.clean_outputs() == 0
    assert not story.exists()
    assert not manifest.exists()
    assert retained.read_text() == "unrelated resource"
