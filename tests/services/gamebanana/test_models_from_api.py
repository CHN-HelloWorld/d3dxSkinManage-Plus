import json
from pathlib import Path

import pytest

from src.services.gamebanana.models import (
    GbAuthor,
    GbCategory,
    GbInstallMetadata,
    GbModel,
    GbModFile,
    GbModProfile,
    GbModRecord,
    GbModUpdateEntry,
    _extract_thumbnail,
)

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.parametrize("model", [GbModRecord, GbModProfile])
@pytest.mark.parametrize("updated", [None, 0, 1700100000])
def test_online_record_prefers_update_date_and_falls_back_to_publication(model, updated):
    payload = {"_tsDateAdded": 1700000000}
    if updated is not None:
        payload["_tsDateUpdated"] = updated
    record = model.from_api(payload)
    assert record.date_updated_ts == (updated or payload["_tsDateAdded"])


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class TestGbAuthorFromApi:
    def test_happy(self):
        payload = load("profile_sample.json")["_aSubmitter"]
        a = GbAuthor.from_api(payload)
        assert a.id == 5555
        assert a.name == "Author"
        assert a.avatar_url.startswith("https://")
        assert a.profile_url.startswith("https://")

    def test_missing_avatar(self):
        a = GbAuthor.from_api({"_idRow": 1, "_sName": "x"})
        assert a.avatar_url == ""


class TestGbModFileFromApi:
    def test_happy(self):
        f = GbModFile.from_api(load("profile_sample.json")["_aFiles"][0])
        assert f.file_id == 999
        assert f.size_bytes == 5_000_000
        assert f.md5_checksum == "deadbeefdeadbeefdeadbeefdeadbeef"
        assert f.av_result == "clean"


class TestGbModRecordFromApi:
    def test_happy(self):
        r = GbModRecord.from_api(load("profile_sample.json"))
        assert r.mod_id == 123456
        assert r.model is GbModel.MOD
        assert r.name == "Sample Mod"
        assert r.submitter.name == "Author"
        assert r.thumbnail_url.startswith("https://")
        assert r.is_nsfw is False


class TestGbModProfileFromApi:
    def test_happy(self):
        p = GbModProfile.from_api(load("profile_sample.json"))
        assert p.mod_id == 123456
        assert p.description_html == "<p>Hello</p>"
        assert len(p.preview_images) == 1
        assert len(p.files) == 1
        assert p.category_id == 5000
        assert p.category_name == "Skins"


class TestGbCategoryFromApi:
    def test_happy(self):
        cats = load("categories_game_a.json")["_aRecords"]
        c = GbCategory.from_api(cats[0])
        assert c.id > 0
        assert c.name
        assert c.item_count >= 0


class TestGbModUpdateEntryFromApi:
    def test_happy(self):
        records = load("updates_sample.json")["_aRecords"]
        u = GbModUpdateEntry.from_api(records[0])
        assert u.title
        assert u.date_added_ts > 0
        assert isinstance(u.tags, list)


class TestGbInstallMetadataRoundtrip:
    def test_to_from_dict(self):
        m = GbInstallMetadata(
            mod_id=1,
            file_id=2,
            file_md5="x",
            file_ts=3,
            page_url="u",
            version=None,
            submitter_id=5,
            submitter_name="a",
            last_checked_iso="2026-01-01T00:00:00",
        )
        d = m.to_dict()
        m2 = GbInstallMetadata.from_dict(d)
        assert m2 == m


class TestExtractThumbnail:
    """验证卡片缩略图的分辨率选择顺序。

    优先使用 530 像素版本，以满足高 DPI 显示下的卡片清晰度；缺失时使用可用的较小图像。
    """

    BASE = "https://images.gamebanana.com/img/ss/mods"

    def _payload(self, **size_fields) -> dict:
        img = {"_sType": "screenshot", "_sBaseUrl": self.BASE, **size_fields}
        return {"_aPreviewMedia": {"_aImages": [img]}}

    def test_prefers_530_over_220(self):
        url = _extract_thumbnail(
            self._payload(
                _sFile="raw.jpg",
                _sFile220="220_x.jpg",
                _sFile530="530_x.jpg",
            )
        )
        assert url == f"{self.BASE}/530_x.jpg"

    def test_falls_back_to_220_when_530_missing(self):
        # GameBanana 偶尔缺 530 变体（早期上传或小图）。降级链：530 → 220 → 800 → raw。
        url = _extract_thumbnail(
            self._payload(
                _sFile="raw.jpg",
                _sFile220="220_x.jpg",
                _sFile800="800_x.jpg",
            )
        )
        assert url == f"{self.BASE}/220_x.jpg"

    def test_falls_back_to_800_when_530_and_220_missing(self):
        url = _extract_thumbnail(
            self._payload(
                _sFile="raw.jpg",
                _sFile800="800_x.jpg",
            )
        )
        assert url == f"{self.BASE}/800_x.jpg"

    def test_falls_back_to_raw_when_all_sizes_missing(self):
        url = _extract_thumbnail(self._payload(_sFile="raw.jpg"))
        assert url == f"{self.BASE}/raw.jpg"

    def test_returns_empty_when_no_preview_media(self):
        assert _extract_thumbnail({}) == ""
        assert _extract_thumbnail({"_aPreviewMedia": {}}) == ""
        assert _extract_thumbnail({"_aPreviewMedia": {"_aImages": []}}) == ""
