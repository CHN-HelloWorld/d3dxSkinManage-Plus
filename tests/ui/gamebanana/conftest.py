from unittest.mock import AsyncMock

import pytest
from PySide6.QtGui import QPixmap

from src.services.gamebanana.client import GameBananaClient
from src.services.gamebanana.models import GbAuthor, GbModel, GbModRecord


@pytest.fixture
def fake_image_loader():
    class _Fake:
        def load(self, url, target, *, size=None, placeholder=None, crop_to_size=False):
            target.setPixmap(placeholder or QPixmap(64, 64))

        def cancel_for(self, target):
            pass

    return _Fake()


@pytest.fixture
def fake_gb_client():
    return AsyncMock(spec=GameBananaClient)


@pytest.fixture
def sample_record():
    return GbModRecord(
        mod_id=123,
        model=GbModel.MOD,
        name="Sample Mod",
        submitter=GbAuthor(id=1, name="A", avatar_url="", profile_url=""),
        thumbnail_url="https://x/thumb.jpg",
        page_url="https://gamebanana.com/mods/123",
        view_count=100,
        like_count=10,
        download_count=5,
        comment_count=2,
        is_nsfw=False,
        date_updated_ts=1700000000,
    )
