"""管理器现有命令、单组合键校验和快捷键配置事务（不依赖 Qt）。"""

from __future__ import annotations

import re
from dataclasses import dataclass

from src.core.config import Config
from src.utils.i18n import N_
from src.utils.i18n import t as tr


@dataclass(frozen=True)
class ShortcutCommand:
    """描述快捷键命令的身份、标题、默认绑定、分组及生效焦点上下文。"""

    id: str
    group: str
    title: str
    description: str
    default: str
    contexts: frozenset[str]

    @property
    def display_title(self) -> str:
        """返回命令显示标题，内置命令使用翻译，插件标题直接显示。"""
        return self.title if self.group.startswith("plugin:") else tr(self.title)

    @property
    def display_description(self) -> str:
        """返回命令说明，内置文本执行翻译，插件文本保留原值。"""
        return self.description if self.group.startswith("plugin:") else tr(self.description)


# 每个快捷键复用现有应用操作，分组仅影响显示。
COMMANDS = (
    ShortcutCommand(
        "edit.copy",
        "general",
        N_("复制"),
        N_("复制选中的文本，或在预设列表中复制预设"),
        "Ctrl+C",
        frozenset({"text", "preset"}),
    ),
    ShortcutCommand(
        "edit.cut",
        "general",
        N_("剪切"),
        N_("剪切选中的内容，或准备移动所选项目"),
        "Ctrl+X",
        frozenset({"text", "overview"}),
    ),
    ShortcutCommand(
        "edit.paste",
        "general",
        N_("粘贴"),
        N_("将已复制或剪切的内容粘贴到当前可接收的位置"),
        "Ctrl+V",
        frozenset({"text", "overview", "preset"}),
    ),
    ShortcutCommand(
        "edit.select_all",
        "general",
        N_("全选"),
        N_("选择当前编辑区域或列表中的全部内容"),
        "Ctrl+A",
        frozenset({"text", "overview", "preset"}),
    ),
    ShortcutCommand(
        "edit.delete",
        "general",
        N_("删除"),
        N_("删除当前选中的内容或项目，保留相应的删除确认"),
        "Delete",
        frozenset({"text", "overview"}),
    ),
    ShortcutCommand(
        "view.refresh",
        "general",
        N_("刷新"),
        N_("刷新当前游戏的 Mod 库，重新核对文件变化"),
        "F5",
        frozenset({"window"}),
    ),
    ShortcutCommand(
        "mod.import",
        "mods",
        N_("导入 Mod"),
        N_("导入到当前类别，需先激活游戏并进入目标类别"),
        "Ctrl+I",
        frozenset({"window"}),
    ),
    ShortcutCommand(
        "view.search",
        "general",
        N_("搜索当前页面"),
        N_("在支持搜索的页面聚焦搜索框并选中已有内容"),
        "Ctrl+F",
        frozenset({"window"}),
    ),
    ShortcutCommand(
        "settings.search", "general", N_("搜索设置"), N_("打开设置并聚焦设置搜索框"), "Ctrl+K", frozenset({"window"})
    ),
    ShortcutCommand(
        "nav.settings",
        "navigation",
        N_("打开设置"),
        N_("前往设置，保留未保存游戏配置的确认"),
        "Ctrl+,",
        frozenset({"window"}),
    ),
    ShortcutCommand("nav.game", "navigation", N_("前往游戏"), N_("打开游戏管理页面"), "Ctrl+1", frozenset({"window"})),
    ShortcutCommand(
        "nav.overview",
        "navigation",
        N_("前往类别总览"),
        N_("打开当前游戏的类别与 Mod 列表"),
        "Ctrl+2",
        frozenset({"window"}),
    ),
    ShortcutCommand(
        "nav.gallery", "navigation", N_("前往画廊"), N_("打开当前游戏的预览画廊"), "Ctrl+3", frozenset({"window"})
    ),
    ShortcutCommand(
        "nav.store",
        "navigation",
        N_("前往在线 Mod"),
        N_("打开当前游戏的 GameBanana 页面"),
        "Ctrl+4",
        frozenset({"window"}),
    ),
    ShortcutCommand(
        "nav.back",
        "navigation",
        N_("返回上一级"),
        N_("返回上级类别，或关闭在线 Mod 详情面板"),
        "Alt+Left",
        frozenset({"window"}),
    ),
    ShortcutCommand(
        "category.new",
        "mods",
        N_("新建类别"),
        N_("在类别总览的当前目录内新建类别"),
        "Ctrl+Shift+N",
        frozenset({"window"}),
    ),
    ShortcutCommand(
        "selection.edit",
        "mods",
        N_("编辑所选项目"),
        N_("编辑列表中选中的单个 Mod 或类别"),
        "F2",
        frozenset({"overview"}),
    ),
    ShortcutCommand(
        "mod.toggle",
        "mods",
        N_("启用或禁用 Mod"),
        N_("切换列表中单个所选 Mod 的启用状态，多选时不执行"),
        "Space",
        frozenset({"overview"}),
    ),
    ShortcutCommand(
        "selection.export",
        "mods",
        N_("导出所选项目"),
        N_("导出列表中选中的 Mod 和类别"),
        "Ctrl+Shift+E",
        frozenset({"overview"}),
    ),
    ShortcutCommand(
        "launch.loader",
        "launch",
        N_("启动 Mod 加载器"),
        N_("启动当前游戏已配置的 Mod 加载器"),
        "F6",
        frozenset({"window"}),
    ),
    ShortcutCommand(
        "launch.game", "launch", N_("启动游戏"), N_("启动当前游戏已配置的可执行文件"), "F7", frozenset({"window"})
    ),
)
COMMAND_BY_ID = {command.id: command for command in COMMANDS}
GROUPS = (
    ("general", N_("通用"), "actions/keyboard"),
    ("navigation", N_("页面导航"), "navigation/overview"),
    ("mods", N_("Mod 与类别"), "navigation/overview"),
    ("launch", N_("启动"), "actions/launch"),
)
MODIFIERS = ("Ctrl", "Alt", "Shift")
_SPECIAL_KEYS = (
    "Space",
    "Delete",
    "Backspace",
    "Insert",
    "Home",
    "End",
    "PageUp",
    "PageDown",
    "Left",
    "Right",
    "Up",
    "Down",
    "Tab",
    "Enter",
    "Esc",
)
_ALIASES = {key.lower(): key for key in (*MODIFIERS, *_SPECIAL_KEYS)}
_ALIASES.update(
    {
        "control": "Ctrl",
        "escape": "Esc",
        "return": "Enter",
        "del": "Delete",
        "pgup": "PageUp",
        "pgdown": "PageDown",
        "pgdn": "PageDown",
        "ins": "Insert",
    }
)
_MAIN_KEY = re.compile(r"^(?:[A-Z0-9]|F[1-9]|F1[0-2]|[,./;'\[\]\\\-=\x60])$")
_RESERVED = frozenset({"Alt+Tab", "Alt+F4", "Alt+Esc", "Ctrl+Esc", "Ctrl+Shift+Esc", "Ctrl+Alt+Delete"})


def normalize_binding(value: object) -> str:
    """转换成稳定的 Ctrl → Alt → Shift 顺序；非法结构返回空字符串。"""
    if not isinstance(value, str) or not value or len(value) > 60:
        return ""
    parts = [_ALIASES.get(part.strip().lower(), part.strip().upper()) for part in value.split("+")]
    main, modifiers = parts[-1], parts[:-1]
    if (
        not main
        or main in MODIFIERS
        or len(modifiers) != len(set(modifiers))
        or any(part not in MODIFIERS for part in modifiers)
    ):
        return ""
    if main not in _SPECIAL_KEYS and not _MAIN_KEY.fullmatch(main):
        return ""
    return "+".join([part for part in MODIFIERS if part in modifiers] + [main])


def validate_binding(value: object, command: ShortcutCommand) -> str:
    """返回可展示的不可用原因；空字符串表示合法。不接受直接清除绑定。"""
    binding = normalize_binding(value)
    if not binding:
        return tr("请录入一个有效的组合键，支持 Ctrl / Alt / Shift 与单个按键。")
    if binding in _RESERVED:
        return tr("这个组合键由系统使用，请选择其他按键。")
    *modifiers, main = binding.split("+")
    strong = "Ctrl" in modifiers or "Alt" in modifiers
    if not strong and main in {"Esc", "Enter", "Tab"}:
        return tr("Esc、Enter、Tab 用于取消、确认和焦点移动，请选择其他按键。")
    if not strong and not re.fullmatch(r"F\d+", main) and main not in {"Space", "Delete"}:
        return tr("请加入 Ctrl 或 Alt，避免普通文字输入时误触发。")
    if not strong and main in {"Space", "Delete"} and "window" in command.contexts:
        return tr("Space 和 Delete 的单键绑定仅用于列表操作，请加入 Ctrl 或 Alt。")
    return ""


def overlapping(first: ShortcutCommand, second: ShortcutCommand) -> bool:
    """判断两个命令是否可能在相同焦点环境生效，窗口级上下文与所有环境交叠。"""
    return "window" in first.contexts or "window" in second.contexts or bool(first.contexts & second.contexts)


class ShortcutStore:
    """唯一配置源为 Config；读取不写盘，事务只修改快捷键差异值。"""

    def __init__(self, config: Config):
        """绑定配置服务，初始化插件命令登记表及最近保存错误。"""
        self.config = config
        self.last_error = ""
        self._plugins: dict[str, tuple[str, tuple[ShortcutCommand, ...]]] = {}

    @property
    def commands(self) -> tuple[ShortcutCommand, ...]:
        """按插件所有者顺序合并内置与插件命令，返回完整命令序列。"""
        return COMMANDS + tuple(command for owner in sorted(self._plugins) for command in self._plugins[owner][1])

    @property
    def command_by_id(self) -> dict[str, ShortcutCommand]:
        """将当前全部命令映射为按身份查询的字典。"""
        return {command.id: command for command in self.commands}

    @property
    def groups(self):
        """返回内置分组与按所有者排序的插件分组及其导航图标。"""
        return GROUPS + tuple(
            ("plugin:" + owner, self._plugins[owner][0], "navigation/plugins") for owner in sorted(self._plugins)
        )
