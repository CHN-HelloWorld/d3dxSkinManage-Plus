"""通过按需命令收集快照，再把清单导出提交给宿主管理的后台任务。"""

from __future__ import annotations

from functools import partial

from PySide6.QtWidgets import QApplication, QFileDialog

from src.plugin_sdk import BackgroundTask, PluginBase, PluginContext, TaskResult

from .inventory import export_inventory


class Plugin(PluginBase):
    """提供工具箱和分类菜单入口，以共享配置控制导出范围。"""

    def activate(self, ctx: PluginContext) -> None:
        """注册命令和任务状态，激活阶段不查询资源库。"""
        self._ctx = ctx
        self._active = True
        self._task: BackgroundTask[str] | None = None
        ctx.commands.register(ctx.plugin_id + ".export", self._export)

    def _export(self, node_id: str = "", **_) -> BackgroundTask[str] | None:
        """在宿主线程校验范围和取得快照，把纯数据及目标路径交给后台。"""
        if not self._active:
            return None
        if self._task is not None and not self._task.done:
            return self._task
        ctx = self._ctx
        tr = ctx.i18n.t
        game_id = ctx.game.current_game_id()
        if not game_id:
            ctx.ui.notify(tr("请先选择一个游戏。"), "warning")
            return None
        node = ctx.game.get_character(node_id) if node_id else None
        if node_id and (node is None or node.game_id != game_id):
            ctx.ui.notify(tr("分类已不存在，请重新选择。"), "warning")
            return None
        mods = [mod for mod in ctx.mods.get_mods() if mod.game_id == game_id]
        characters = ctx.game.get_characters()
        enabled_only = ctx.configuration.get("enabled_only")
        path, _ = QFileDialog.getSaveFileName(
            QApplication.activeWindow(), tr("导出 Mod 清单"), "mod-inventory.csv", tr("CSV 文件 (*.csv)")
        )
        # 文件对话框运行嵌套事件循环，返回时插件可能已经停用。
        if not path or not self._active:
            return None
        worker = partial(
            export_inventory, mods=mods, characters=characters, path=path, node_id=node_id, enabled_only=enabled_only
        )
        self._task = ctx.tasks.run(worker, title=tr("导出 Mod 清单"))
        ctx.subscriptions.add(self._task.on_finished(self._on_finished))
        return self._task

    def _on_finished(self, result: TaskResult[str]) -> None:
        """在宿主线程报告最终结果，后台异常保持为可重试的任务失败。"""
        self._task = None
        if not self._active:
            return
        tr = self._ctx.i18n.t
        if result.cancelled:
            self._ctx.ui.notify(tr("清单导出已取消。"), "warning")
        elif not result.ok:
            self._ctx.ui.notify(tr("清单导出失败：{error}", error=result.error), "error")
        else:
            self._ctx.ui.notify(tr("Mod 清单已导出。"), "success")

    def deactivate(self) -> None:
        """停止发布结果；宿主负责取消该插件的任务并移除旧回调。"""
        self._active = False
        if self._task is not None:
            self._task.cancel()
        self._task = None
