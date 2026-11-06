"""注册按需权限修复入口，激活阶段不扫描目录或修改权限。"""

from __future__ import annotations

from functools import partial

from src.plugin_sdk import PluginBase, PluginContext


class Plugin(PluginBase):
    """管理一次性修复窗口，卸载或停用时取消尚未完成的工作。"""

    def activate(self, ctx: PluginContext) -> None:
        """只注册插件操作命令，不在应用启动或插件启用时运行修复。"""
        self._ctx = ctx
        self._active = True
        self._panel = None
        self._dialog = None
        self._generation = 0
        ctx.commands.register(ctx.plugin_id + ".open", self._open)

    def _open(self, **_):
        """按需创建宿主管理的非模态窗口，重复打开时复用现有实例。"""
        if not self._active:
            return None
        if self._dialog is not None:
            self._dialog.show()
            self._dialog.raise_()
            self._dialog.activateWindow()
            return self._dialog
        from .panel import RepairPanel

        self._generation += 1
        generation = self._generation
        panel = RepairPanel(self._ctx)
        dialog = self._ctx.ui.show_dialog(panel, title=self._ctx.i18n.t("目录权限修复"), modal=False)
        if dialog is None:
            panel.shutdown()
            panel.deleteLater()
            return None
        self._panel, self._dialog = panel, dialog
        panel.configure_window(dialog)
        dialog.finished.connect(partial(self._finished, generation))
        dialog.destroyed.connect(partial(self._destroyed, generation))
        return dialog

    def _finished(self, generation: int, _result: int) -> None:
        """关闭窗口时停止订阅和工作，再释放宿主持有的对话框。"""
        if generation != self._generation:
            return
        panel, dialog = self._panel, self._dialog
        self._panel = self._dialog = None
        if panel is not None:
            panel.shutdown()
        if dialog is not None:
            dialog.deleteLater()

    def _destroyed(self, generation: int, *_args) -> None:
        """处理宿主直接销毁窗口的情况，防止任务保留已失效的控件引用。"""
        if generation == self._generation:
            panel = self._panel
            self._panel = self._dialog = None
            if panel is not None:
                panel.shutdown()

    def deactivate(self) -> None:
        """先阻止迟到回调，再停止任务和关闭修复窗口。"""
        self._active = False
        self._generation += 1
        panel, dialog = self._panel, self._dialog
        self._panel = self._dialog = None
        if panel is not None:
            panel.shutdown()
        if dialog is not None:
            dialog.close()
            dialog.deleteLater()
