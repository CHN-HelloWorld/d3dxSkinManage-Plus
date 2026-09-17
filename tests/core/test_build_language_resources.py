from argparse import Namespace
from importlib import metadata
from pathlib import Path
from zipfile import ZipFile

import pytest


def test_nuitka_version_can_be_read_without_starting_a_subprocess(monkeypatch):
    from scripts.release import build

    monkeypatch.setattr(metadata, "version", lambda name: "4.2.0")

    def blocked_probe(*args, **kwargs):
        raise AssertionError("Version inspection must not start a compiler process")

    monkeypatch.setattr(build.subprocess, "run", blocked_probe)
    assert build.nuitka_version() == (4, 2, 0)


@pytest.mark.parametrize("onefile", [False, True])
def test_language_configs_are_not_embedded_in_either_executable(onefile):
    from scripts.release import build

    command = build.build_command(Namespace(onefile=onefile, console=False, nuitka_args=[]))
    assert not any(str(build.LANGUAGES) in option for option in command)
    assert (build.LANGUAGES / "zh-CN/strings.json").is_file()
    assert (build.LANGUAGES / "en-US/strings.json").is_file()


@pytest.mark.parametrize("onefile", [False, True])
def test_default_languages_are_distributed_beside_both_executable_formats(tmp_path, monkeypatch, onefile):
    from scripts.release import build

    monkeypatch.setattr(build, "PROJECT_BUILD", tmp_path)
    product = tmp_path / "product"
    product.mkdir()
    executable = product / "app.exe"
    executable.write_bytes(b"program")
    directory = build.stage_language_packs(product)
    assert directory == product / "config/languages"
    assert sorted(
        str(path.relative_to(directory)).replace("\\", "/") for path in directory.rglob("*") if path.is_file()
    ) == sorted(build.SHIPPED_LANGUAGE_FILES)
    entries = [(executable, ""), (directory, "config/languages")] if onefile else [(product, "")]
    archive = build.make_zip(entries)
    with ZipFile(archive) as package:
        names = {name.split("/", 1)[1]: name for name in package.namelist()}
        for relative in build.SHIPPED_LANGUAGE_FILES:
            assert package.read(names[f"config/languages/{relative}"]) == (build.LANGUAGES / relative).read_bytes()


def test_release_zip_contains_default_languages_without_local_config(tmp_path, monkeypatch):
    from scripts.release import build

    monkeypatch.setattr(build, "PROJECT_BUILD", tmp_path)
    product = tmp_path / "product"
    files = {
        "app.exe": "program",
        "config/languages/zh-CN/strings.json": "Chinese",
        "config/languages/en-US/strings.json": "English",
        "config/languages/README.md": "Instructions",
        "config/languages/schema.json": "Schema",
        "config/settings.json": "private settings",
        "config/games.json": "private games",
        "config/languages/custom/strings.json": "private translation",
    }
    for relative, contents in files.items():
        path = product / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")
    archive = build.make_zip([(product, "")])
    with ZipFile(archive) as package:
        names = {Path(name).as_posix().split("/", 1)[1] for name in package.namelist()}
    assert names == {
        "app.exe",
        "config/languages/zh-CN/strings.json",
        "config/languages/en-US/strings.json",
        "config/languages/README.md",
        "config/languages/schema.json",
    }
