"""小型卡片控件池，所有权保留在滚动内容中。"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from typing import TypeVar

from PySide6.QtWidgets import QWidget

Card = TypeVar("Card", bound=QWidget)


def suspend_card(card: QWidget) -> None:
    """重新绑定前隐藏卡片，停止引用旧记录的动画。"""
    card.hide()
    for name in ("_move_anim", "_geom_anim", "_stagger_anim", "_scale_anim"):
        animation = getattr(card, name, None)
        if animation is not None:
            try:
                animation.stop()
            except RuntimeError:
                pass
    effect = card.graphicsEffect()
    if effect is not None:
        reset_opacity = getattr(effect, "set_reveal_opacity", None)
        if callable(reset_opacity):
            reset_opacity(1.0)
        if effect is getattr(card, "_scroll_edge_fade_effect", None):
            invalidate = getattr(effect, "invalidate_source", None)
            if callable(invalidate):
                invalidate()
            effect.setEnabled(False)
        else:
            # 尚未执行的错峰显示回调也同时失效。
            card.setGraphicsEffect(None)
            card._scroll_edge_fade_effect = None


class CardPool:
    """按卡片类型保留曾使用的视口容量。"""

    def __init__(self) -> None:
        """创建按控件类型分组的空闲卡片栈。"""
        self._idle: dict[type[QWidget], list[QWidget]] = defaultdict(list)

    def take(self, card_type: type[Card]) -> Card | None:
        """从指定卡片类型的空闲栈取出一个实例，没有可复用实例时返回 None。"""
        cards = self._idle.get(card_type)
        return cards.pop() if cards else None

    def count(self, card_type: type[Card]) -> int:
        """返回指定卡片类型当前可复用的实例数量。"""
        return len(self._idle.get(card_type, ()))

    def release(self, card: QWidget) -> None:
        """暂停卡片活动并按其实际类型放回空闲池，供后续绑定数据复用。"""
        suspend_card(card)
        self._idle[type(card)].append(card)

    def clear(self, dispose: Callable[[QWidget], None]) -> None:
        """调用指定释放函数处理全部空闲卡片，然后清空池内引用。"""
        for cards in self._idle.values():
            for card in cards:
                dispose(card)
        self._idle.clear()
