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
