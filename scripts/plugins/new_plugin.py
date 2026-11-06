"""插件脚手架：生成按需加载的多文件插件、清单、语言包与说明。

用法：
    python scripts/plugins/new_plugin.py <plugin_id> [--name 名称] [--author 作者] [--dir 输出目录]

默认输出到应用插件目录（plugins/<id>/）。生成后在「插件」页「重新扫描」即可发现，
首次启用需确认信任。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 允许从仓库根运行
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.plugin_sdk import API_VERSION
from src.plugins.runtime.manifest import ManifestError, validate_plugin_id
from src.utils.paths import get_plugins_dir

_ENTRY_TEMPLATE = '''"""插件入口：只依赖公开 SDK。"""

from __future__ import annotations

from functools import partial

from src.plugin_sdk import PluginBase, PluginContext

from .commands import show_summary


class Plugin(PluginBase):
    """通过公开 SDK 查询当前游戏并显示 Mod 启用数量。"""

    def activate(self, ctx: PluginContext) -> None:
        """保存插件上下文并注册统计命令。"""
        self._ctx = ctx
        ctx.commands.register({command}, partial(show_summary, ctx))
        ctx.logger.info("插件已激活：%s", ctx.meta.name)

    def deactivate(self) -> None:
        """记录插件停用日志。"""
        self._ctx.logger.info("插件已停用：%s", self._ctx.meta.name)
'''

_COMMANDS_TEMPLATE = '''"""插件命令逻辑，与生命周期入口分别维护。"""

from src.plugin_sdk import PluginContext


def show_summary(ctx: PluginContext, **_) -> dict[str, int]:
    """显示当前游戏的 Mod 总数与启用数，并返回可复用的统计结果。"""
    tr = ctx.i18n.t
    if not ctx.game.current_game_id():
        ctx.ui.notify(tr("请先选择一个游戏。"), "warning")
        return {"mods": 0, "enabled": 0}
    mods = ctx.mods.get_mods()
    enabled = sum(mod.enabled for mod in mods)
    ctx.ui.notify(tr("共 {total} 个 Mod，已启用 {enabled} 个。", total=len(mods), enabled=enabled))
    return {"mods": len(mods), "enabled": enabled}
'''

_README_TEMPLATE = """# {name}

{description}

## 开发

- 入口：`entry.py` 导出 `Plugin` 类（继承 `src.plugin_sdk.PluginBase`）。
- 元数据/权限/贡献点：见 `plugin.json`。
- 多文件逻辑放在 `commands.py`，使用相对导入；宿主为每次加载创建独立包。
- 声明的命令首次执行时自动激活插件，启动时不读取资源库。
- 翻译：`ctx.i18n.t()` 读取 `languages/<语言>/strings.json`，跟随宿主语言。
- 命令：从插件详情的「操作」区或「插件命令」执行「查看 Mod 统计」，结果以通知显示。
- 快捷键：默认 `Ctrl+Alt+M`，可在应用设置中修改；冲突时不会覆盖已有绑定。
- 导入宿主代码时只使用 `src.plugin_sdk` 及其子模块；标准库和 PySide6 仍可按需使用。
- 自绘界面使用 `PySide6`（信号/槽为 `Signal` / `Slot`），把 `QWidget` 交给 `ctx.ui` 托管；
  使用宿主已有的 `QApplication`。
- Python 使用四空格缩进、双引号、现代类型标注和中文文档字符串，按项目 Ruff 配置格式化。
- 实用示例：`example.mod-inventory` 导出清单，`example.preset-audit` 检查引用，
  `example.preset-cycle` 通过异步任务切换预设。
- 完整指南可在应用的 `docs/d3dxSkinManage-Plus 插件开发指南.html` 中离线阅读。

## 安装

运行 `python scripts/plugins/plugin_tool.py check <本目录>` 校验，
再运行 `python scripts/plugins/plugin_tool.py pack <本目录> --output <目录外的发布包.zip>`。
工具只打包 `plugin.json` 的 `files` 声明内容。在应用「插件」页选择压缩包安装，
或将源码放入插件目录后「重新扫描」。
"""


def main() -> int:
    """校验插件标识并在新目录生成清单、入口、语言包和说明文件，已有目录保持不变。"""
    ap = argparse.ArgumentParser(description="生成插件骨架")
    ap.add_argument("plugin_id", help="插件 id（反向域名，如 com.you.myplugin）")
    ap.add_argument("--name", default="")
    ap.add_argument("--author", default="You")
    ap.add_argument("--dir", default="")
    args = ap.parse_args()

    pid = args.plugin_id.strip()
    try:
        validate_plugin_id(pid)
    except ManifestError as exc:
        print(str(exc))
        return 2

    name = args.name or pid.split(".")[-1]
    command = pid + ".summary"
    base = Path(args.dir) if args.dir else get_plugins_dir()
    target = base / pid
    if target.exists():
        print(f"目标已存在: {target}")
        return 1
    target.mkdir(parents=True, exist_ok=True)

    manifest = {
        "id": pid,
        "name": name,
        "version": "0.1.0",
        "apiVersion": API_VERSION,
        "files": ["entry.py", "commands.py", "README.md", "languages"],
        "minAppVersion": "1.0.0",
        "author": args.author,
        "description": f"{name} 插件",
        "entry": "entry.py",
        "permissions": ["mods:read", "ui:shortcuts"],
        "contributes": {
            "commands": [{"id": command, "title": "查看 Mod 统计"}],
            "shortcuts": [{"command": command, "default": "Ctrl+Alt+M", "context": "window"}],
        },
    }
    (target / "plugin.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    (target / "entry.py").write_text(
        _ENTRY_TEMPLATE.format(command=json.dumps(command)), encoding="utf-8", newline="\n"
    )
    (target / "commands.py").write_text(_COMMANDS_TEMPLATE, encoding="utf-8", newline="\n")
    language_dir = target / "languages" / "en-US"
    language_dir.mkdir(parents=True)
    (language_dir / "strings.json").write_text(
        json.dumps(
            {
                "_meta": {"format": 1, "name": "English"},
                "messages": {
                    "查看 Mod 统计": "Show mod summary",
                    "插件": "Plugins",
                    "请先选择一个游戏。": "Select a game first.",
                    "共 {total} 个 Mod，已启用 {enabled} 个。": "{total} mods, {enabled} enabled.",
                },
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    (target / "README.md").write_text(
        _README_TEMPLATE.format(name=name, description=manifest["description"]), encoding="utf-8", newline="\n"
    )

    print(f"已生成插件骨架: {target}")
    print("下一步：在应用「插件」页「重新扫描」并启用。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
