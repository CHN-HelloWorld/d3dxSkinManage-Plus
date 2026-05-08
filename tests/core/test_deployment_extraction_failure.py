from __future__ import annotations

import threading
import zipfile

import pytest

from src.core.mod_package import write_container_marker
from src.core.mod_storage import ModStorage
from src.models.mod import Mod

_IDENTITY = "abcdefabcdef4def8defabcdefabcdef"
_SECOND_PAYLOAD = b"second member payload"


@pytest.fixture
def deployment_source(tmp_path):
    container = tmp_path / "source"
    container.mkdir()
    source = container / "skin.zip"
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("wrapper/first.txt", b"first member")
        archive.writestr("wrapper/second.bin", _SECOND_PAYLOAD)
    assert write_container_marker(
        container,
        source.name,
        Mod.compute_sha1(source),
        mod_id=_IDENTITY,
        imported_at="2026-10-23T00:00:00",
    )
    storage = ModStorage()
    storage.set_migoto_mods_dir(tmp_path / "deploy")
    return storage, source


def test_extraction_failure_reports_error_and_removes_partial(deployment_source):
    storage, source = deployment_source
    # 破坏首个文件之后的成员 CRC，触发真实解压失败，并验证已解出的内容得到清理。
    source.write_bytes(source.read_bytes().replace(_SECOND_PAYLOAD, b"broken member payload"))
    original_source = source.read_bytes()
    active, disabled, partial = storage.deployment_paths(_IDENTITY, "Skin")
    extracted_first_file = []

    def observe_progress(_fraction):
        first = partial / "wrapper/first.txt"
        if first.is_file():
            extracted_first_file.append(first)

    with pytest.raises(OSError, match="解压失败") as error:
        storage.materialize_deployment(
            source,
            _IDENTITY,
            "Skin",
            progress_cb=observe_progress,
            should_cancel=lambda: False,
        )

    assert not isinstance(error.value, InterruptedError)
    assert source.name in str(error.value)
    assert extracted_first_file, "Extraction must create a file before failing"
    assert not any(path.exists() for path in (active, disabled, partial))
    assert source.read_bytes() == original_source


def test_explicit_cancellation_remains_interrupted_and_removes_partial(deployment_source):
    storage, source = deployment_source
    original_source = source.read_bytes()
    active, disabled, partial = storage.deployment_paths(_IDENTITY, "Skin")
    cancellation = threading.Event()

    def cancel_after_first_file(_fraction):
        if (partial / "wrapper/first.txt").is_file():
            cancellation.set()

    with pytest.raises(InterruptedError, match="cancelled"):
        storage.materialize_deployment(
            source,
            _IDENTITY,
            "Skin",
            progress_cb=cancel_after_first_file,
            should_cancel=cancellation.is_set,
        )

    assert cancellation.is_set(), "Cancellation must occur after extraction starts"
    assert not any(path.exists() for path in (active, disabled, partial))
    assert source.read_bytes() == original_source
