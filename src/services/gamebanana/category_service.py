"""按需加载并缓存多级分类。

根分类按游戏保存，子分类按父分类编号保存；切换游戏时通过 invalidate_game 使缓存失效。
"""

from __future__ import annotations

from src.services.gamebanana.client import GameBananaClient
from src.services.gamebanana.models import GbCategory


class GbCategoryService:
    """按游戏和父分类缓存在线分类列表，避免重复请求已展开的分类层级。"""

    def __init__(self, client: GameBananaClient) -> None:
        """借用在线客户端，分别建立游戏根分类和父节点子分类缓存。"""
        self._client = client
        # 按游戏保存根分类列表。
        self._roots: dict[int, list[GbCategory]] = {}
        # 按父分类保存子分类列表。
        self._children: dict[int, list[GbCategory]] = {}

    async def roots(self, game_id: int) -> list[GbCategory]:
        """返回指定游戏的根分类，首次访问时从在线客户端获取并缓存。"""
        if game_id not in self._roots:
            self._roots[game_id] = await self._client.categories(game_id)
        return self._roots[game_id]

    async def children(self, parent_id: int) -> list[GbCategory]:
        """返回指定父分类的直接子分类，首次访问时获取并缓存。"""
        if parent_id not in self._children:
            self._children[parent_id] = await self._client.sub_categories(parent_id)
        return self._children[parent_id]

    def invalidate_game(self, game_id: int) -> None:
        """移除指定游戏根分类，并清空无法按游戏区分的全部子分类缓存。"""
        self._roots.pop(game_id, None)
        # 子分类缓存仅包含父分类编号，无法反查所属游戏，因此切换游戏时全部清除。
        self._children.clear()
