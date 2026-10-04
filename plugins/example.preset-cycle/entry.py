"""按宿主预设顺序切换组合，通过异步任务反馈完成状态。"""

from __future__ import annotations

from src.plugin_sdk import OperationResult, OperationTask, PluginBase, PluginContext
from src.plugin_sdk.presets import PresetInfo


def select_preset(
    presets: list[PresetInfo], current_id: str, step: int, *, include_default: bool = False
) -> PresetInfo | None:
    """在宿主给出的顺序中循环选择，跳过当前唯一项及未纳入的默认预设。"""
    choices = [preset for preset in presets if include_default or not preset.is_default]
    if not choices:
        return None
    current = next((index for index, preset in enumerate(choices) if preset.preset_id == current_id), None)
    if current is None:
        return choices[0 if step > 0 else -1]
    target = choices[(current + step) % len(choices)]
    return target if target.preset_id != current_id else None


class Plugin(PluginBase):
    """提供前后预设命令，等待当前切换完成后才接受下一次请求。"""

    def activate(self, ctx: PluginContext) -> None:
        """初始化任务状态并注册两个可绑定快捷键的命令。"""
        self._ctx = ctx
        self._active = True
        self._task: OperationTask | None = None
        self._target_name = ""
        ctx.commands.register(ctx.plugin_id + ".previous", self._previous)
        ctx.commands.register(ctx.plugin_id + ".next", self._next)

    def _previous(self, **_) -> OperationTask | None:
        """请求切换到上一个符合设置的预设。"""
        return self._switch(-1)

    def _next(self, **_) -> OperationTask | None:
        """请求切换到下一个符合设置的预设。"""
        return self._switch(1)

    def _switch(self, step: int) -> OperationTask | None:
        """校验游戏和候选项，通过宿主队列提交一次预设应用任务。"""
        if not self._active:
            return None
        ctx = self._ctx
        tr = ctx.i18n.t
        if self._task is not None and not self._task.done:
            ctx.ui.notify(tr("预设正在切换，请稍候。"), "warning")
            return self._task
        game_id = ctx.game.current_game_id()
        if not game_id:
            ctx.ui.notify(tr("请先选择一个游戏。"), "warning")
            return None
        current = ctx.presets.get_current()
        target = select_preset(
            [preset for preset in ctx.presets.get_presets() if preset.game_id == game_id],
            current.preset_id if current is not None else "",
            step,
            include_default=ctx.configuration.get("include_default"),
        )
        if target is None:
            ctx.ui.notify(tr("没有其他可切换的预设。请先在主界面创建组合预设。"), "warning")
            return None
        self._target_name = tr("默认") if target.is_default and target.name == "默认" else target.name
        task = ctx.presets.apply(target.preset_id)
        # 已完成的任务会立即调用新回调，必须先保存状态再注册回调。
        self._task = task
        task.on_finished(self._on_finished)
        return task

    def _on_finished(self, result: OperationResult) -> None:
        """释放忙碌状态，仅在宿主确认成功后显示切换完成。"""
        self._task = None
        if not self._active:
            return
        tr = self._ctx.i18n.t
        if result.ok:
            self._ctx.ui.notify(tr("已切换到预设：{name}", name=self._target_name), "success")
        else:
            message = result.error or tr("操作未完成，请查看宿主任务结果。")
            self._ctx.ui.notify(tr("预设切换未完成：{error}", error=message), "error")

    def deactivate(self) -> None:
        """先禁止反馈，再请求取消尚未完成的任务；宿主负责移除任务回调。"""
        self._active = False
        if self._task is not None and not self._task.done:
            self._task.cancel()
        self._task = None
