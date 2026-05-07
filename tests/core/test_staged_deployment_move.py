import os
import zipfile
from pathlib import Path

import pytest

from src.core.mod_package import write_container_marker
from src.core.mod_storage import ModStorage
from src.models.mod import Mod


@pytest.mark.parametrize("locked_phase", ["flatten", "publish"])
def test_materialize_retries_transient_windows_directory_lock(tmp_path, monkeypatch, locked_phase):
    container = tmp_path / "source"
    container.mkdir()
    body = container / "skin.zip"
    with zipfile.ZipFile(body, "w") as archive:
        archive.writestr("wrapper/skin/mod.ini", "[Constants]\nx=1\n")
    identity = "abcdefabcdef4def8defabcdefabcdef"
    assert write_container_marker(
        container, body.name, Mod.compute_sha1(body), mod_id=identity, imported_at="2026-10-07T00:00:00"
    )
    storage = ModStorage()
    storage.set_migoto_mods_dir(tmp_path / "deploy")
    calls = []
    rename, replace = os.rename, os.replace

    def blocked(operation, source, target):
        phase = "publish" if Path(source).name.endswith(".partial") else "flatten"
        if phase == locked_phase and len(calls) < 2:
            calls.append(phase)
            error = PermissionError("directory temporarily in use")
            error.winerror = 5
            raise error
        return operation(source, target)

    monkeypatch.setattr(os, "rename", lambda s, t: blocked(rename, s, t))
    monkeypatch.setattr(os, "replace", lambda s, t: blocked(replace, s, t))
    deployed = storage.materialize_deployment(body, identity, "Skin")
    assert (deployed / "skin/mod.ini").read_text() == "[Constants]\nx=1\n"
    assert calls == [locked_phase, locked_phase]
    assert not any(p.name.endswith(".partial") for p in deployed.parent.iterdir())
