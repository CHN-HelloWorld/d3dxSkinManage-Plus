"""逐张显示缓存卡片，不在布局阶段消耗显示延迟。"""

from time import perf_counter

from PySide6.QtCore import QEvent, QObject, QTimer

from src.ui import anim


class CardRevealQueue(QObject):
    """按绘制完成状态和时间间隔依次推进卡片显现动画。"""

    TICK_MS = 16
    STEP_SECONDS = 0.026
    FADE_SECONDS = 0.18

    def __init__(self, owner, fade, is_current):
        """保存淡入效果与卡片有效性回调，创建逐帧推进队列的定时器。"""
        super().__init__(owner)
        self._fade = fade
        self._is_current = is_current
        self._cards = []
        self._previous = None
        self._last_start = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(self.TICK_MS)
        self._timer.timeout.connect(self._advance)

    def is_active(self):
        """返回显现动画的推进定时器是否正在运行。"""
        return self._timer.isActive()

    def add(self, card, key):
        """为可见绑定排队，回收控件使用新的请求项。"""
        if not anim.is_enabled():
            return
        for index, (current, old_effect, old_key, _start) in enumerate(self._cards):
            if current is card:
                if old_key == key:
                    return
                self._cards.pop(index)
                if self._previous is old_effect:
                    self._previous = None
                break
        effect = self._fade.prepare_card(card)
        if effect is None:
            return
        effect.set_reveal_opacity(0.0)
        card.installEventFilter(self)
        self._cards.append((card, effect, key, None))
        if not self._timer.isActive():
            self._timer.start()

    def eventFilter(self, obj, event):
        """在鼠标按下或键盘输入时取消渐显效果，并继续传递原始输入事件。"""
        if event.type() in (QEvent.Type.MouseButtonPress, QEvent.Type.KeyPress):
            self.cancel()
        return False

    def cancel(self):
        """停止动画计时，将剩余卡片恢复为完全可见并清空队列状态。"""
        self._timer.stop()
        for _card, effect, _key, _started in self._cards:
            try:
                effect.set_reveal_opacity(1.0)
            except RuntimeError:
                pass
        self._cards.clear()
        self._previous = None
        self._last_start = 0.0

    def _advance(self):
        """检查卡片绑定与可见性，按时间间隔和上一张实际绘制状态准入下一张卡片。

        使用缓出曲线更新透明度；动画禁用、父控件隐藏或队列耗尽时停止计时。
        """
        if not anim.is_enabled() or not self.parent().isVisible():
            self.cancel()
            return
        now = perf_counter()
        admit = now - self._last_start >= self.STEP_SECONDS and (
            self._previous is None or self._previous.reveal_painted
        )
        remaining = []
        for card, effect, key, started in self._cards:
            try:
                if card.isHidden() or card.graphicsEffect() is not effect or not self._is_current(card, key):
                    effect.set_reveal_opacity(1.0)
                    if effect is self._previous:
                        self._previous = None
                    continue
                if started is None:
                    if not admit:
                        remaining.append((card, effect, key, None))
                        continue
                    started = now
                    self._last_start = now
                    self._previous = effect
                    admit = False
                progress = min(1.0, max(0.0, (now - started) / self.FADE_SECONDS))
                effect.set_reveal_opacity(max(1 / 255, 1 - (1 - progress) ** 3))
                if progress < 1.0:
                    remaining.append((card, effect, key, started))
            except RuntimeError:
                if effect is self._previous:
                    self._previous = None
        self._cards = remaining
        if not remaining:
            self._timer.stop()
