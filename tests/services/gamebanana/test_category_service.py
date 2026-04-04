from unittest.mock import AsyncMock

import pytest

from src.services.gamebanana.category_service import GbCategoryService
from src.services.gamebanana.models import GbCategory


def _cat(cid, name, parent=None):
    return GbCategory(id=cid, name=name, parent_id=parent, item_count=10, icon_url=None)


@pytest.fixture
def fake_client():
    c = AsyncMock()
    c.categories.return_value = [_cat(5000, "Skins"), _cat(5001, "Effects")]
    c.sub_categories.return_value = [_cat(5100, "Characters", 5000)]
    return c


async def test_roots_cached_after_first_call(fake_client):
    svc = GbCategoryService(fake_client)
    a = await svc.roots(101)
    b = await svc.roots(101)
    assert a == b
    assert fake_client.categories.call_count == 1


async def test_children_cached_per_parent(fake_client):
    svc = GbCategoryService(fake_client)
    a = await svc.children(5000)
    b = await svc.children(5000)
    assert a == b
    assert fake_client.sub_categories.call_count == 1


async def test_invalidate_game_clears_roots_and_children(fake_client):
    svc = GbCategoryService(fake_client)
    await svc.roots(101)
    await svc.children(5000)
    svc.invalidate_game(101)
    await svc.roots(101)
    await svc.children(5000)
    assert fake_client.categories.call_count == 2
    assert fake_client.sub_categories.call_count == 2
