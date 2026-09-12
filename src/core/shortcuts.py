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

    def register_plugin(self, owner: str, title: str, commands) -> None:
        """验证插件命令身份、上下文和默认绑定，并为冲突默认键保存禁用覆盖。

        需要保存的覆盖写入成功后才替换插件登记，失败时保留原命令集合并报告错误。"""
        definitions = tuple(commands)
        occupied = set(COMMAND_BY_ID)
        for other, (_, registered) in self._plugins.items():
            if other != owner:
                occupied.update(command.id for command in registered)
        for command in definitions:
            if not command.id or command.id in occupied:
                raise ValueError(tr("快捷键命令 ID 已被占用：{command_id}", command_id=command.id))
            if command.group != "plugin:" + owner or command.contexts not in (
                frozenset({"window"}),
                frozenset({"overview"}),
                frozenset({"preset"}),
            ):
                raise ValueError(tr("插件快捷键必须使用本插件分组和受支持的焦点上下文"))
            error = validate_binding(command.default, command)
            if error:
                raise ValueError(error)
            occupied.add(command.id)

        overrides = self.config.get("shortcuts.overrides")
        if not isinstance(overrides, dict):
            overrides = {}
        else:
            overrides = dict(overrides)
        previous_ids = {command.id for command in self._plugins.get(owner, ("", ()))[1]}
        incumbent_by_id = {command.id: command for command in self.commands if command.id not in previous_ids}
        active = [
            (incumbent_by_id[command_id], binding)
            for command_id, binding in self.bindings.items()
            if command_id in incumbent_by_id and binding is not None
        ]
        changed = False
        for command in definitions:
            if command.id in overrides:
                value = overrides[command.id]
                if value is not None:
                    value = command.default if validate_binding(value, command) else normalize_binding(value)
            else:
                value = command.default
                if any(value == binding and overlapping(command, incumbent) for incumbent, binding in active):
                    # 先持久化新命令的默认冲突处理，再发布命令；用户绑定始终保留优先级。
                    overrides[command.id] = None
                    value = None
                    changed = True
            if value is not None:
                active.append((command, value))

        if changed and not self.config.set_atomic("shortcuts.overrides", overrides):
            self.last_error = tr("快捷键保存失败，已保持原有绑定。请检查设置文件是否只读或被占用后重试。")
            raise RuntimeError(self.last_error)
        self._plugins[owner] = (title, definitions)
        self.last_error = ""

    def unregister_plugin(self, owner: str) -> None:
        """移除指定插件的运行时命令登记，保留用户配置供后续重新加载。"""
        self._plugins.pop(owner, None)

    @property
    def bindings(self) -> dict[str, str | None]:
        """按有效用户覆盖优先生成全部绑定，规范无效配置并禁用上下文交叠的重复按键。"""
        overrides = self.config.get("shortcuts.overrides")
        if not isinstance(overrides, dict):
            overrides = {}
        result: dict[str, str | None] = {}
        # 已有用户绑定优先于新增默认绑定。
        commands = self.commands
        by_id = self.command_by_id
        ordered = sorted(
            commands,
            key=lambda command: (
                not (
                    command.id in overrides
                    and overrides[command.id] is not None
                    and not validate_binding(overrides[command.id], command)
                )
            ),
        )
        for command in ordered:
            value = overrides.get(command.id, command.default)
            if value is not None:
                value = command.default if validate_binding(value, command) else normalize_binding(value)
                # 手工编辑配置也不能在同一上下文同时激活两个命令；不改写原文件。
                if any(value == key and overlapping(command, by_id[other]) for other, key in result.items()):
                    value = None
            result[command.id] = value
        return {command.id: result[command.id] for command in commands}

    def binding(self, command_id: str) -> str | None:
        """返回指定命令当前有效的快捷键，None 表示未绑定。"""
        return self.bindings[command_id]

    @property
    def custom_count(self) -> int:
        """统计当前有效绑定与各命令默认值不同的命令数量。"""
        bindings = self.bindings
        return sum(bindings[command.id] != command.default for command in self.commands)

    def conflicts(self, command_id: str, binding: str) -> list[ShortcutCommand]:
        """查找与候选按键相同且焦点环境交叠的其他命令。"""
        key = normalize_binding(binding)
        command = self.command_by_id[command_id]
        bindings = self.bindings
        return [
            other
            for other in self.commands
            if other.id != command_id and key and bindings[other.id] == key and overlapping(command, other)
        ]

    def save_binding(self, command_id: str, binding: str | None, *, replace: bool = False) -> bool:
        """验证并保存指定命令的绑定，可按调用方选择解除冲突命令；失败时记录原因。"""
        command = self.command_by_id[command_id]
        self.last_error = validate_binding(binding, command)
        if self.last_error:
            return False
        key = normalize_binding(binding)
        conflicts = self.conflicts(command_id, key)
        if conflicts and not replace:
            self.last_error = tr("该组合键已被其他操作使用，请确认替换后再保存。")
            return False
        candidate = self.bindings
        for other in conflicts:
            candidate[other.id] = None
        candidate[command_id] = key
        return self._persist(candidate)

    def restore_defaults(self) -> bool:
        """将当前已登记命令的绑定恢复为默认值并持久化。"""
        return self._persist({command.id: command.default for command in self.commands})

    def restore(self, snapshot: dict[str, str | None]) -> bool:
        """撤销整个快捷键事务；不恢复快照创建后修改的其他设置。"""
        candidate: dict[str, str | None] = {}
        by_id = self.command_by_id
        for command in self.commands:
            value = snapshot.get(command.id, command.default)
            if value is not None:
                error = validate_binding(value, command)
                if error:
                    self.last_error = error
                    return False
                value = normalize_binding(value)
                if any(value == key and overlapping(command, by_id[other]) for other, key in candidate.items()):
                    self.last_error = tr("待恢复的快捷键存在冲突。")
                    return False
            candidate[command.id] = value
        return self._persist(candidate)

    def _persist(self, candidate: dict[str, str | None]) -> bool:
        """仅保存相对默认值的覆盖项，原子写入失败时保留错误信息并返回 False。"""
        overrides = self.config.get("shortcuts.overrides")
        if not isinstance(overrides, dict):
            overrides = {}
        for command in self.commands:
            if candidate[command.id] == command.default:
                overrides.pop(command.id, None)
            else:
                overrides[command.id] = candidate[command.id]
        if not self.config.set_atomic("shortcuts.overrides", overrides):
            self.last_error = tr("快捷键保存失败，已保持原有绑定。请检查设置文件是否只读或被占用后重试。")
            return False
        self.last_error = ""
        return True
