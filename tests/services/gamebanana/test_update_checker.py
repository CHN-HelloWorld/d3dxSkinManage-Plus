from src.services.gamebanana.models import (
    GbInstallMetadata,
    GbModFile,
    GbModUpdateEntry,
)
from src.services.gamebanana.update_checker import (
    UpdateInfo,
    detect_update,
    pick_default_file,
)


def _meta(file_ts=1000, file_md5="aaa", file_id=10, version="v1.0"):
    return GbInstallMetadata(
        mod_id=123,
        file_id=file_id,
        file_md5=file_md5,
        file_ts=file_ts,
        page_url="https://gamebanana.com/mods/123",
        version=version,
        submitter_id=1,
        submitter_name="A",
        last_checked_iso="",
    )


def _file(file_id, ts, md5, name="skin.zip", ver=""):
    return GbModFile(
        file_id=file_id,
        file_name=name,
        description="",
        download_url="http://x/" + name,
        size_bytes=100,
        date_added_ts=ts,
        md5_checksum=md5,
        av_result="clean",
        version=ver,
    )


def test_up_to_date_when_no_newer_file():
    meta = _meta(file_ts=1000, file_md5="aaa")
    files = [_file(10, 1000, "aaa")]  # 同一个文件
    assert detect_update(meta, None, files, [], set()) is None


def test_desc_only_change_not_reported():
    meta = _meta(file_ts=1000, file_md5="aaa")
    files = [_file(10, 2000, "aaa")]  # 新时间戳但内容指纹相同
    assert detect_update(meta, None, files, [], set()) is None


def test_real_update_reported():
    meta = _meta(file_ts=1000, file_md5="aaa", version="v1.0")
    files = [_file(11, 2000, "bbb", name="skin_v2.zip", ver="v1.3")]
    info = detect_update(meta, None, files, [], set())
    assert info is not None
    assert info.latest_file.file_id == 11
    assert info.latest_version == "v1.3"
    assert info.current_version == "v1.0"
    assert info.page_url == "https://gamebanana.com/mods/123"


def test_ignored_file_id_skipped():
    meta = _meta(file_ts=1000, file_md5="aaa")
    files = [_file(11, 2000, "bbb")]
    assert detect_update(meta, None, files, [], {11}) is None


def test_multi_candidate_picks_newest_and_lists_all():
    meta = _meta(file_ts=1000, file_md5="aaa")
    files = [
        _file(11, 2000, "bbb", name="standard.zip"),
        _file(12, 3000, "ccc", name="alternate.zip"),
    ]
    info = detect_update(meta, None, files, [], set())
    assert info.latest_file.file_id == 12  # 最新
    assert {f.file_id for f in info.candidate_files} == {11, 12}


def test_no_version_falls_back_empty_string():
    meta = _meta(version=None)
    files = [_file(11, 2000, "bbb", ver="")]
    info = detect_update(meta, None, files, [], set())
    assert info.latest_version == ""
    assert info.current_version is None


def test_changelog_only_newer_than_current():
    meta = _meta(file_ts=1000)
    files = [_file(11, 2000, "bbb")]
    updates = [
        GbModUpdateEntry(title="old", date_added_ts=500, description_html="x", tags=[]),
        GbModUpdateEntry(title="new", date_added_ts=2000, description_html="y", tags=[]),
    ]
    info = detect_update(meta, None, files, updates, set())
    assert [u.title for u in info.changelog] == ["new"]


def test_pick_default_file_by_name_similarity():
    cands = [
        _file(11, 2000, "bbb", name="StandardBody.zip"),
        _file(12, 2000, "ccc", name="AlternateBody.zip"),
    ]
    chosen = pick_default_file(cands, "AlternateBody")
    assert chosen.file_id == 12


def test_update_info_roundtrip():
    meta = _meta()
    files = [_file(11, 2000, "bbb", ver="v2")]
    updates = [GbModUpdateEntry(title="t", date_added_ts=2000, description_html="<p>h</p>", tags=["x"])]
    info = detect_update(meta, None, files, updates, set())
    info.local_mod_id = "mid1"
    info.local_name = "示例角色丁"
    d = info.to_dict()
    back = UpdateInfo.from_dict(d)
    assert back.local_mod_id == "mid1"
    assert back.local_name == "示例角色丁"
    assert back.latest_file.file_id == 11
    assert back.latest_file.md5_checksum == "bbb"
    assert back.changelog[0].description_html == "<p>h</p>"


# 仅有来源 URL 时，以未知基线检查可用文件。


def test_unknown_baseline_reports_latest_file():
    """本地版本、文件时间和摘要均未知时，报告最新可用文件。"""
    meta = _meta(file_ts=0, file_md5="", version=None)
    files = [_file(11, 1700000000, "bbb", ver="1.4.3")]
    info = detect_update(meta, None, files, [], set())
    assert info is not None
    assert info.latest_file.file_id == 11
    assert info.latest_version == "1.4.3"
    assert info.current_version is None


def test_unknown_baseline_same_version_is_up_to_date():
    meta = _meta(file_ts=0, file_md5="", version="1.4.3")
    files = [_file(11, 1700000000, "bbb", ver="1.4.3")]
    assert detect_update(meta, None, files, [], set()) is None
