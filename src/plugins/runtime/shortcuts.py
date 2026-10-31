"""将插件声明的命令接入宿主统一的快捷键分发器。"""

from functools import partial

from src.core.shortcuts import ShortcutCommand
from src.plugin_sdk.errors import PermissionDenied
from src.utils.i18n import t as tr


class PluginShortcutHost:
    """协调插件快捷键在配置模型和窗口分发器中的注册与注销。"""

    def __init__(self, settings, dispatcher) -> None:
        """绑定快捷键配置及分发器，并初始化插件归属集合。"""
        self.settings = settings
        self.dispatcher = dispatcher
        self._owners: set[str] = set()

    def register(self, manifest, commands, translate) -> None:
        """校验权限和命令归属后注册快捷键；任一环节失败时撤销本次插件注册。"""
        shortcuts = manifest.contributes.get("shortcuts", [])
        if not shortcuts:
            return
        if "ui:shortcuts" not in manifest.permissions:
            raise PermissionDenied("ui:shortcuts")
        declarations = {item["id"]: item for item in manifest.contributes.get("commands", [])}
        definitions = []
        for item in shortcuts:
            command_id = item["command"]
            if commands.owner(command_id) != manifest.id:
                raise ValueError(tr("快捷键命令尚未由本插件注册：{command_id}", command_id=command_id))
            declaration = declarations[command_id]
            definitions.append(
                ShortcutCommand(
                    command_id,
                    "plugin:" + manifest.id,
                    translate(manifest.id, declaration.get("title", command_id)),
                    translate(manifest.id, declaration.get("description", "")),
                    item["default"],
                    frozenset({item.get("context", "window")}),
                )
            )
        try:
            for command in definitions:
                self.dispatcher.register_plugin_action(
                    manifest.id, command.id, partial(self._execute, commands, manifest.id, command.id)
                )
            self.settings.register_plugin(manifest.id, translate(manifest.id, manifest.name), definitions)
            self._owners.add(manifest.id)
        except Exception:
            self.dispatcher.unregister_plugin(manifest.id)
            self.settings.unregister_plugin(manifest.id)
            raise

    @staticmethod
    def _execute(commands, owner, command_id):
        """仅在命令仍属于原插件时执行，防止注销或替换后的过期快捷键触发。"""
        if commands.owner(command_id) == owner:
            return commands.execute(command_id)
        return None

    def unregister(self, plugin_id: str) -> None:
        """同时撤销插件在分发器和快捷键配置中的注册。"""
        if plugin_id in self._owners:
            self._owners.remove(plugin_id)
            self.dispatcher.unregister_plugin(plugin_id)
            self.settings.unregister_plugin(plugin_id)
