"""按位置提供插件菜单项。

宿主注入提供者，各入口分别请求 Mod、类别或全局命令。
返回文案与回调列表，无对应扩展时返回空列表。
"""

from __future__ import annotations

from collections.abc import Callable

# provider(location: str, context_id: str) -> list[tuple[str, Callable[[], None]]]
_Provider = Callable[[str, str], list]
_provider: _Provider | None = None


def set_provider(fn: _Provider | None) -> None:
    """注册全局插件菜单提供者；传入 None 时撤销提供者。"""
    global _provider
    _provider = fn


def menu_actions(location: str, context_id: str = "") -> list:
    """按菜单位置和上下文标识查询插件动作，未注册或查询异常时返回空列表。"""
    if _provider is None:
        return []
    try:
        return list(_provider(location, context_id) or [])
    except Exception:
        return []
