"""验证分享元数据保留更新基线，并支持未填写版本号的 GameBanana 资源。"""

from __future__ import annotations

from src.core.mod_package import _shareable_custom_data, share_meta_from_mod
from src.models.mod import Mod, ModInfo, ModStatus
from src.services.gamebanana.models import GbInstallMetadata, GbModFile
from src.services.gamebanana.update_checker import detect_update
from src.services.gamebanana.url_helper import parse_gb_url


def _store_mod(*, version: str, gb: dict | None, url: str = "") -> Mod:
    """构造一个「商店下载」的本地 Mod（可控版本号 / GameBanana 来源）。"""
    info = ModInfo(version=version, url=url, custom_data={})
    if gb is not None:
        info.custom_data["gamebanana"] = gb
    return Mod(
        game_id="game_a",
        mod_id="local1",
        name="Tau",
        folder_name="tau",
        status=ModStatus.DISABLED,
        info=info,
    )


def _newer_file() -> GbModFile:
    """一个比本地基线更新、且内容指纹不同的远端文件。"""
    return GbModFile(
        file_id=999,
        file_name="tau_v2.zip",
        description="",
        download_url="http://x/v2.zip",
        size_bytes=100,
        date_added_ts=2000,
        md5_checksum="bbbbbb",
        av_result="clean",
        version="",
    )


def test_no_version_structured_meta_survives_export_and_detects_update():
    gb = {
        "mod_id": 310004,
        "file_id": 100,
        "file_md5": "aaaaaa",
        "file_ts": 1000,
        "page_url": "https://gamebanana.com/mods/310004",
        "version": "",
        "submitter_id": 7,
        "submitter_name": "Author",
        "last_checked_iso": "2026-01-01T00:00:00",
    }
    mod = _store_mod(version="", gb=gb)

    # 1) 导出：share_meta_from_mod 抽取可分享元数据
    meta = share_meta_from_mod(mod)
    shared_gb = meta["info"]["custom_data"]["gamebanana"]
    # 时间戳、摘要和文件标识共同构成更新检测的基线。
    assert shared_gb["mod_id"] == 310004
    assert shared_gb["file_id"] == 100
    assert shared_gb["file_md5"] == "aaaaaa"
    assert shared_gb["file_ts"] == 1000
    assert shared_gb["version"] == ""  # 版本号为空被如实携带
    assert shared_gb["last_checked_iso"] == ""  # 接收方从「未检测」开始

    # 2) 导入：_resolve_update_source 在 mod_id 存在时直接用结构化基线
    restored = GbInstallMetadata.from_dict(shared_gb)

    # 3) 检查更新：基于 ts+md5 指纹判定，版本号为空不影响
    info = detect_update(restored, None, [_newer_file()], [], set())
    assert info is not None, "无版本号但有精确基线时应能检测到更新"
    assert info.latest_file.file_id == 999

    # 远端文件摘要未变时不报告更新。
    same = GbModFile(
        file_id=100,
        file_name="tau.zip",
        description="",
        download_url="http://x/v1.zip",
        size_bytes=100,
        date_added_ts=1000,
        md5_checksum="aaaaaa",
        av_result="clean",
        version="",
    )
    assert detect_update(restored, None, [same], [], set()) is None


def test_no_version_no_modid_falls_back_to_url_and_detects_update():
    mod = _store_mod(version="", gb=None, url="https://gamebanana.com/mods/310004")

    # 导出信封携带 info.url（无 custom_data.gamebanana，因为没有 mod_id）
    meta = share_meta_from_mod(mod)
    assert meta["info"].get("url") == "https://gamebanana.com/mods/310004"
    assert "custom_data" not in meta["info"] or not meta["info"]["custom_data"]

    # 导入后 _resolve_update_source 的 url 回退路径：构造「基线未知」最小元数据
    parsed = parse_gb_url(meta["info"]["url"])
    assert parsed is not None
    _model, mod_id = parsed
    restored = GbInstallMetadata(
        mod_id=mod_id,
        file_id=0,
        file_md5="",
        file_ts=0,
        page_url=meta["info"]["url"],
        version=(mod.info.version or None),
        submitter_id=0,
        submitter_name="",
        last_checked_iso="",
    )
    # 基线未知且版本号为空时，报告最新可用文件。
    info = detect_update(restored, None, [_newer_file()], [], set())
    assert info is not None, "url 回退 + 空版本号时应仍能检测到更新"
    assert info.latest_file.file_id == 999


def test_shareable_custom_data_dropped_when_no_modid():
    assert _shareable_custom_data({"gamebanana": {"version": "v1"}}) == {}
    out = _shareable_custom_data({"gamebanana": {"mod_id": 5, "file_ts": 9}})
    assert out["gamebanana"]["mod_id"] == 5
    assert out["gamebanana"]["last_checked_iso"] == ""
