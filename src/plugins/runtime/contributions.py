"""贡献点注册表（宿主侧）。

汇总已经通过授权和依赖检查的插件贡献点（commands/toolsPage/menus/settings/views），
供宿主 UI 渲染时查询。准备阶段登记元数据，停用时移除，无需预先执行插件代码。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.plugins.runtime.manifest import Manifest


@dataclass(frozen=True)
class Contribution:
    """单条贡献点（已绑定来源插件）。"""

    plugin_id: str
    plugin_name: str
    kind: str  # 贡献类型：命令、工具页、菜单、设置或视图。
    data: dict[str, Any]

    # 便捷访问
    @property
    def command(self) -> str:
        """从命令引用或声明 ID 取得贡献对应的命令标识。"""
        return str(self.data.get("command", self.data.get("id", "")))

    @property
    def title(self) -> str:
        """返回贡献标题，未声明时使用命令标识。"""
        return str(self.data.get("title", self.command))

    @property
    def icon(self) -> str:
        """返回贡献声明的图标名称，未声明时为空字符串。"""
        return str(self.data.get("icon", ""))


class ContributionRegistry:
    """可用插件的声明式贡献点汇总。"""

    def __init__(self, *, translate=None) -> None:
        # 按贡献类型保存注册项。
        """初始化按类型索引的贡献集合，绑定可选的插件翻译函数。"""
        self._by_kind: dict[str, list[Contribution]] = {}
        self._translate = translate or (lambda plugin_id, source: source)

    # 维护

    def add(self, mf: Manifest) -> None:
        """登记通过清单与依赖检查的全部贡献点。"""
        self.remove(mf.id)
        contributes = mf.contributes or {}
        # 先建命令标题表：toolsPage/menus 项只带 command，显示标题取自 commands 贡献
        cmd_titles: dict[str, str] = {}
        for it in contributes.get("commands") or []:
            if isinstance(it, dict) and it.get("id"):
                cmd_titles[str(it["id"])] = str(it.get("title", it["id"]))

        def _enrich(item: dict) -> dict:
            """复制贡献记录，并从对应命令声明补充缺失标题。"""
            d = dict(item)
            if "title" not in d:
                cmd = str(d.get("command", ""))
                if cmd in cmd_titles:
                    d["title"] = cmd_titles[cmd]
            return d

        for kind, items in contributes.items():
            if not isinstance(items, list):
                # menus 是 dict(location -> list)；单独处理
                if kind == "menus" and isinstance(items, dict):
                    for location, sub in items.items():
                        for it in sub or []:
                            if isinstance(it, dict):
                                merged = _enrich(it)
                                merged.setdefault("location", location)
                                self._by_kind.setdefault("menus", []).append(
                                    Contribution(mf.id, mf.name, "menus", merged)
                                )
                continue
            for it in items:
                if isinstance(it, dict):
                    self._by_kind.setdefault(kind, []).append(Contribution(mf.id, mf.name, kind, _enrich(it)))

    def remove(self, plugin_id: str) -> None:
        """移除某插件的全部贡献点（停用/卸载时调用）。"""
        for kind in list(self._by_kind.keys()):
            self._by_kind[kind] = [c for c in self._by_kind[kind] if c.plugin_id != plugin_id]

    def clear(self) -> None:
        """清除全部插件贡献记录。"""
        self._by_kind.clear()

    # 查询

    def _localized(self, items) -> list[Contribution]:
        """复制并翻译贡献的展示字段，保留原始注册数据以支持语言切换。"""
        result = []
        for item in items:
            data = dict(item.data)
            for key in ("title", "description", "group"):
                if isinstance(data.get(key), str):
                    data[key] = self._translate(item.plugin_id, data[key])
            if "enumTitles" in data:
                data["enumTitles"] = [self._translate(item.plugin_id, title) for title in data["enumTitles"]]
            result.append(
                Contribution(item.plugin_id, self._translate(item.plugin_id, item.plugin_name), item.kind, data)
            )
        return result

    def tools_page(self) -> list[Contribution]:
        """返回工具页贡献的本地化副本。"""
        return self._localized(self._by_kind.get("toolsPage", []))

    def menus(self, location: str | None = None) -> list[Contribution]:
        """按可选菜单位置筛选贡献，并返回本地化副本。"""
        items = self._by_kind.get("menus", [])
        if location is None:
            return self._localized(items)
        return self._localized(c for c in items if c.data.get("location") == location)

    def settings(self, plugin_id: str | None = None) -> list[Contribution]:
        """返回全部或指定插件的本地化设置贡献。"""
        items = self._by_kind.get("settings", [])
        if plugin_id is None:
            return self._localized(items)
        return self._localized(c for c in items if c.plugin_id == plugin_id)

    def commands(self) -> list[Contribution]:
        """返回命令声明的本地化副本。"""
        return self._localized(self._by_kind.get("commands", []))

    def views(self, plugin_id: str | None = None) -> list[Contribution]:
        """返回全部或指定插件的本地化视图贡献。"""
        items = self._by_kind.get("views", [])
        if plugin_id is None:
            return self._localized(items)
        return self._localized(c for c in items if c.plugin_id == plugin_id)
