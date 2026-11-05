"""按需打开旧版迁移向导，激活阶段只注册命令和生命周期事件。"""

from __future__ import annotations

from functools import partial

from src.plugin_sdk import PluginBase, PluginContext


class Plugin(PluginBase):
    """把迁移窗口及其后台任务绑定到当前插件实例。"""

    def activate(self, ctx: PluginContext) -> None:
        """注册工具箱入口，不读取旧版目录或查询游戏资源库。"""
        self._ctx = ctx
        self._active = True
        self._wizard = None
        self._dialog = None
        self._dialog_generation = 0
        ctx.commands.register(ctx.plugin_id + ".open", self._open)
        ctx.events.on("game_changed", self._game_changed)

    def _open(self, **_):
        """创建宿主管理的非模态窗口，重复命令仅激活已有窗口。"""
        if not self._active:
            return None
        if self._dialog is not None:
            self._dialog.show()
            self._dialog.raise_()
            self._dialog.activateWindow()
            return self._dialog
        from .wizard import MigrationWizard

        self._dialog_generation += 1
        generation = self._dialog_generation
        wizard = MigrationWizard(self._ctx)
        self._wizard = wizard
        dialog = self._ctx.ui.show_dialog(wizard, title=self._ctx.i18n.t("从旧版迁移"), modal=False)
        if dialog is None:
            wizard.shutdown()
            wizard.deleteLater()
            self._wizard = None
            return None
        self._dialog = dialog
        dialog.finished.connect(partial(self._dialog_finished, generation))
        dialog.destroyed.connect(partial(self._dialog_destroyed, generation))
        return dialog

    def _game_changed(self, game_id: str = "", **_) -> None:
        """游戏切换后使旧预览失效，并停止旧目标的后续提交。"""
        if self._active and self._wizard is not None:
            self._wizard.controller.game_changed(game_id)

    def _dialog_finished(self, generation: int, _result: int) -> None:
        """关闭窗口立即取消本次任务，再交给 Qt 释放控件。"""
        if generation != self._dialog_generation:
            return
        wizard, dialog = self._wizard, self._dialog
        self._wizard = self._dialog = None
        if wizard is not None:
            wizard.shutdown()
        if dialog is not None:
            dialog.deleteLater()

    def _dialog_destroyed(self, generation: int, *_args) -> None:
        """清除宿主直接销毁窗口后留下的 Python 引用。"""
        if generation == self._dialog_generation:
            self._wizard = self._dialog = None

    def deactivate(self) -> None:
        """先停止回调和任务，再关闭宿主持有的窗口。"""
        self._active = False
        self._dialog_generation += 1
        wizard, dialog = self._wizard, self._dialog
        self._wizard = self._dialog = None
        if wizard is not None:
            wizard.shutdown()
        if dialog is not None:
            dialog.close()
            dialog.deleteLater()
