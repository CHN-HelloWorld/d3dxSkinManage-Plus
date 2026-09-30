from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

import src.services.archive as archive_backend
from src.services.archive import ArchiveService
from tests.helpers import registered_games as registered_games

_PAYLOAD = b"update-validation-payload:" + bytes(range(256)) * 257


def _archive(root: Path, extension: str, *, stored=False) -> Path:
    source = root / "Skin"
    source.mkdir()
    (source / "mod.ini").write_text("[Constants]\nx=42\n", encoding="utf-8")
    (source / "texture.buf").write_bytes(_PAYLOAD)
    (source / "empty.txt").touch()
    output = root / f"update.{extension}"
    service = ArchiveService()
    if extension == "7z" and not archive_backend.PY7ZR_AVAILABLE:
        pytest.skip("py7zr is unavailable")
    if extension == "rar" and not archive_backend.rar_compress_available():
        pytest.skip("Creating a real RAR requires the project's existing rar.exe backend")
    if stored and extension == "zip":
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
            for file in source.iterdir():
                archive.write(file, f"Skin/{file.name}")
    elif stored and extension == "7z":
        with archive_backend.py7zr.SevenZipFile(
            output,
            "w",
            filters=[{"id": archive_backend.py7zr.FILTER_COPY}],
        ) as archive:
            archive.writeall(source, "Skin")
    else:
        assert getattr(service, f"compress_to_{extension}")(source, output)
    return output


@pytest.mark.parametrize("extension", ["zip", "7z", "rar"])
def test_valid_supported_archives_are_checked_without_writing_files(tmp_path, extension):
    from src.core.mod_update_validation import validate_update_archive

    archive = _archive(tmp_path, extension)
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    before_paths = set(tmp_path.rglob("*"))
    assert validate_update_archive(archive, ArchiveService()) is None
    assert {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == before
    assert set(tmp_path.rglob("*")) == before_paths


@pytest.mark.parametrize("extension", ["zip", "7z", "rar"])
def test_text_disguised_as_an_archive_is_rejected(tmp_path, extension):
    from src.core.mod_update_validation import validate_update_archive

    archive = tmp_path / f"download.{extension}"
    archive.write_bytes(b"<html>download failed, please try again</html>")
    with pytest.raises(ValueError, match="校验失败") as error:
        validate_update_archive(archive, ArchiveService())
    assert archive.name in str(error.value)


@pytest.mark.parametrize("extension", ["zip", "7z", "rar"])
def test_truncated_archive_is_rejected(tmp_path, extension):
    from src.core.mod_update_validation import validate_update_archive

    archive = _archive(tmp_path, extension)
    content = archive.read_bytes()
    archive.write_bytes(content[: len(content) // 2])
    with pytest.raises(ValueError, match="校验失败"):
        validate_update_archive(archive, ArchiveService())


@pytest.mark.parametrize("extension", ["zip", "7z"])
def test_crc_damage_in_non_ini_payload_is_rejected_even_when_headers_are_readable(tmp_path, extension):
    from src.core.mod_update_validation import validate_update_archive

    archive = _archive(tmp_path, extension, stored=True)
    content = bytearray(archive.read_bytes())
    payload_at = content.find(_PAYLOAD)
    assert payload_at >= 0, "the fixture stores the member without compression"
    content[payload_at + len(_PAYLOAD) // 2] ^= 0x01
    archive.write_bytes(content)
    service = ArchiveService()
    assert service.list_entries(archive) is not None, "header-only content hashing still accepts the damaged file"
    with pytest.raises(ValueError, match="校验失败"):
        validate_update_archive(archive, service)


@pytest.mark.parametrize("damage_earlier_member", [False, True])
def test_duplicate_zip_names_do_not_hide_an_earlier_damaged_member(tmp_path, damage_earlier_member):
    from src.core.mod_update_validation import validate_update_archive

    archive = tmp_path / "duplicates.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as output:
        output.writestr("Skin/texture.buf", _PAYLOAD)
        with pytest.warns(UserWarning, match="Duplicate name"):
            output.writestr("Skin/texture.buf", b"later replacement")
    if damage_earlier_member:
        content = bytearray(archive.read_bytes())
        payload_at = content.find(_PAYLOAD)
        assert payload_at >= 0
        content[payload_at + len(_PAYLOAD) // 2] ^= 1
        archive.write_bytes(content)
        with zipfile.ZipFile(archive) as source:
            assert source.testzip() is None, "the standard helper reads the later name twice"
        with pytest.raises(ValueError, match="校验失败"):
            validate_update_archive(archive, ArchiveService())
    else:
        assert validate_update_archive(archive, ArchiveService()) is None


@pytest.mark.parametrize("extension", ["zip", "7z"])
def test_empty_valid_archive_is_not_confused_with_a_failed_listing(tmp_path, extension):
    from src.core.mod_update_validation import validate_update_archive

    archive = tmp_path / f"empty.{extension.upper()}"
    if extension == "zip":
        with zipfile.ZipFile(archive, "w"):
            pass
    else:
        if not archive_backend.PY7ZR_AVAILABLE:
            pytest.skip("py7zr is unavailable")
        with archive_backend.py7zr.SevenZipFile(archive, "w"):
            pass
    assert validate_update_archive(archive, ArchiveService()) is None


def test_unavailable_7z_backend_cannot_silently_accept_an_archive(tmp_path, monkeypatch):
    from src.core.mod_update_validation import validate_update_archive

    archive = _archive(tmp_path, "7z")
    monkeypatch.setattr(archive_backend, "PY7ZR_AVAILABLE", False)
    with pytest.raises(ValueError, match="7z.*后端"):
        validate_update_archive(archive, ArchiveService())


def test_missing_archive_has_an_explicit_error(tmp_path):
    from src.core.mod_update_validation import validate_update_archive

    with pytest.raises(ValueError, match="不存在"):
        validate_update_archive(tmp_path / "missing.zip", ArchiveService())
