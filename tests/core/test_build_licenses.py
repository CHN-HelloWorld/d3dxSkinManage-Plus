import csv
import io
import json
import subprocess
import sys
from importlib import metadata
from types import SimpleNamespace
from zipfile import ZipFile

import pytest
from packaging.utils import canonicalize_name


@pytest.fixture
def license_environment(tmp_path, monkeypatch):
    from scripts.release import build

    source = tmp_path / "source"
    source.mkdir()
    (source / "LICENSE").write_bytes(b"Project license\r\n")
    (source / "THIRD_PARTY_NOTICES.md").write_bytes(b"Project notices\r\n")
    supplements = source / "resources/licenses"
    supplements.mkdir(parents=True)
    (supplements / "sources.json").write_text("{}\n", encoding="utf-8")
    python = tmp_path / "python"
    python.mkdir()
    (python / "LICENSE.txt").write_bytes(b"Python runtime license\r\n")
    site = tmp_path / "site-packages"
    distributions = {}

    def install(name, *, requires=(), declarations=("LICENSE.txt",), files=None, package_files=None):
        stem = canonicalize_name(name).replace("-", "_") + "-1.0.dist-info"
        directory = site / stem
        directory.mkdir(parents=True, exist_ok=True)
        headers = ["Metadata-Version: 2.4", f"Name: {name}", "Version: 1.0", "License-Expression: MIT"]
        headers.extend(f"Requires-Dist: {requirement}" for requirement in requires)
        headers.extend(f"License-File: {filename}" for filename in declarations)
        records = {f"{stem}/METADATA": ("\n".join(headers) + "\n").encode()}
        if files is None:
            files = {"licenses/LICENSE.txt": f"{name} license\r\n".encode()}
        records.update({f"{stem}/{relative}": data for relative, data in files.items()})
        records.update(package_files or {})
        for relative, data in records.items():
            path = site / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        rows = io.StringIO()
        csv.writer(rows, lineterminator="\n").writerows((relative, "", "") for relative in records)
        (directory / "RECORD").write_text(rows.getvalue(), encoding="utf-8")
        distribution = metadata.PathDistribution(directory)
        distributions[canonicalize_name(name)] = distribution
        return distribution

    def distribution(name):
        try:
            return distributions[canonicalize_name(name)]
        except KeyError:
            raise metadata.PackageNotFoundError(name) from None

    monkeypatch.setattr(metadata, "distribution", distribution)
    monkeypatch.setattr(build, "ROOT", source)
    monkeypatch.setattr(sys, "base_prefix", str(python))
    return SimpleNamespace(
        install=install, product=tmp_path / "product", python=python, site=site, build=build, supplements=supplements
    )


def test_stages_runtime_dependencies_and_later_requested_extras(license_environment):
    env = license_environment
    env.install(
        env.build.APP_NAME,
        requires=(
            "shared>=1.0",
            "plugin",
            'pytest; extra == "dev"',
            'Nuitka; extra == "build"',
            'foreign; sys_platform == "not-a-platform"',
        ),
    )
    env.install("shared", requires=("base", 'codec; extra == "render"', 'test-only; extra == "test"'))
    env.install("plugin", requires=("shared[render]>=1.0",))
    for name in ("base", "codec", "pytest", "Nuitka", "foreign", "test-only", "unrelated"):
        env.install(name)

    env.build.stage_legal_notices(env.product)

    for name in ("shared", "plugin", "base", "codec"):
        assert (env.product / f"licenses/{name}/LICENSE.txt").read_bytes() == f"{name} license\r\n".encode()
    assert {path.name for path in (env.product / "licenses").iterdir() if path.is_dir()} == {
        "shared",
        "plugin",
        "base",
        "codec",
        "python",
    }
    assert (env.product / "licenses/python/LICENSE.txt").read_bytes() == b"Python runtime license\r\n"
    assert (env.product / "LICENSE").read_bytes() == b"Project license\r\n"
    assert (env.product / "THIRD_PARTY_NOTICES.md").read_bytes() == b"Project notices\r\n"
    index = json.loads((env.product / "licenses/index.json").read_text(encoding="utf-8"))
    assert {package["name"] for package in index["packages"]} == {"shared", "plugin", "base", "codec"}


def test_requested_extra_keeps_base_dependencies(license_environment):
    env = license_environment
    env.install(env.build.APP_NAME, requires=("library[render]",))
    env.install("library", requires=('base; extra == ""', 'codec; extra == "render"'))
    env.install("base")
    env.install("codec")

    env.build.stage_legal_notices(env.product)

    assert (env.product / "licenses/base/LICENSE.txt").read_bytes() == b"base license\r\n"
    assert (env.product / "licenses/codec/LICENSE.txt").read_bytes() == b"codec license\r\n"


def test_copies_declared_legacy_and_embedded_wheel_notices_unchanged(license_environment):
    env = license_environment
    env.install(env.build.APP_NAME, requires=("PySide6_Essentials", "unrar2-cffi", "httpx", "watchdog"))
    env.install(
        "PySide6_Essentials",
        declarations=("LicenseRef-Qt-Commercial.txt",),
        files={"licenses/LicenseRef-Qt-Commercial.txt": b"Qt commercial notice\r\n"},
        package_files={"PySide6/Qt/licenses/LGPL-3.0.txt": b"Qt bundled LGPL text\r\n"},
    )
    env.install(
        "unrar2-cffi",
        declarations=("LICENSE",),
        files={"licenses/LICENSE": b"Binding license\n"},
        package_files={"unrar/LICENSE-UnRAR.txt": b"\xef\xbb\xbfUnRAR engine notice\r\n"},
    )
    env.install("httpx", declarations=(), files={"licenses/LICENSE.md": b"HTTP license\n"})
    env.install(
        "watchdog",
        declarations=("LICENSE", "COPYING", "AUTHORS"),
        files={"LICENSE": b"Watchdog license", "COPYING": b"Watchdog terms", "AUTHORS": b"Watchdog authors"},
    )

    env.build.stage_legal_notices(env.product)

    expected = {
        "pyside6-essentials/LicenseRef-Qt-Commercial.txt": b"Qt commercial notice\r\n",
        "pyside6-essentials/PySide6/Qt/licenses/LGPL-3.0.txt": b"Qt bundled LGPL text\r\n",
        "unrar2-cffi/LICENSE": b"Binding license\n",
        "unrar2-cffi/unrar/LICENSE-UnRAR.txt": b"\xef\xbb\xbfUnRAR engine notice\r\n",
        "httpx/LICENSE.md": b"HTTP license\n",
        "watchdog/LICENSE": b"Watchdog license",
        "watchdog/COPYING": b"Watchdog terms",
        "watchdog/AUTHORS": b"Watchdog authors",
    }
    for relative, content in expected.items():
        assert (env.product / "licenses" / relative).read_bytes() == content


@pytest.mark.parametrize("same_content", [False, True])
def test_repeated_wheel_notice_paths_are_deduplicated_only_when_identical(license_environment, same_content):
    env = license_environment
    env.install(env.build.APP_NAME, requires=("library",))
    env.install(
        "library",
        declarations=("library/vendor/LICENSE",),
        files={"licenses/library/vendor/LICENSE": b"Vendor terms\r\n"},
        package_files={"library/vendor/LICENSE": b"Vendor terms\r\n" if same_content else b"Other vendor terms\r\n"},
    )
    if same_content:
        env.build.stage_legal_notices(env.product)
        assert (env.product / "licenses/library/library/vendor/LICENSE").read_bytes() == b"Vendor terms\r\n"
        index = json.loads((env.product / "licenses/index.json").read_text(encoding="utf-8"))
        assert len(index["packages"][0]["files"]) == 1
    else:
        with pytest.raises(ValueError, match="library"):
            env.build.stage_legal_notices(env.product)


@pytest.mark.parametrize("missing", ["declared", "recorded", "all", "python", "dependency"])
def test_missing_license_material_aborts_staging(license_environment, missing):
    env = license_environment
    env.install(env.build.APP_NAME, requires=("library",))
    if missing == "dependency":
        expected = "library"
    elif missing == "declared":
        env.install("library", declarations=("LICENSE.txt", "COPYING"))
        expected = "COPYING"
    elif missing == "all":
        env.install("library", declarations=(), files={})
        expected = "library"
    else:
        env.install("library")
        if missing == "recorded":
            (env.site / "library-1.0.dist-info/licenses/LICENSE.txt").unlink()
            expected = "LICENSE.txt"
        else:
            (env.python / "LICENSE.txt").unlink()
            expected = "Python"

    with pytest.raises((OSError, ValueError), match=expected):
        env.build.stage_legal_notices(env.product)
