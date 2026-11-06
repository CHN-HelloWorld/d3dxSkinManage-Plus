"""配置管理 - 单例模式"""

from __future__ import annotations

import json
from collections.abc import Callable
from copy import deepcopy
from typing import Any

from src.core.archive_concurrency import MAX_ARCHIVE_WORKERS
from src.core.config_file_io import backup_invalid_file, read_json, write_json_atomic
from src.core.deploy_concurrency import MAX_DEPLOY_CONCURRENCY
from src.core.image_memory_policy import (
    DEFAULT_MANUAL_IMAGE_CACHE_MB,
    MAX_IMAGE_CACHE_MB,
    MIN_IMAGE_CACHE_MB,
)
from src.core.mod_update_concurrency import MAX_UPDATE_CONCURRENCY
from src.utils.logger import logger
from src.utils.paths import get_config_dir, resolve_stored_path, to_stored_path

# settings.json 的结构版本。

# 版本标记仅用于诊断，不决定配置是否有效。
# 读取时按键使用内置默认值，保留未知键，单项无效值不影响其他配置。
SETTINGS_SCHEMA_VERSION = 1

_MISSING = object()


class Config:
    """程序偏好设置的单例管理器，读写 config/settings.json。

    管理主题、语言、导入导出、界面、代理及更新策略。游戏列表、激活游戏、当前预设和
    社区映射由 GameConfigStore 独立保存，恢复默认设置不会影响这些用户数据。

    读取顺序为用户值、内置默认值、调用方默认值。缺省项不写回用户文件。
    无效单项或配置段仅影响自身；整份 JSON 无法解析时备份原文件再初始化。
    版本字段用于标识文件，不作为拒绝读取的依据。
    """

    _instance: Config | None = None

    _DEFAULT_SETTINGS: dict[str, Any] = {
        "schema_version": SETTINGS_SCHEMA_VERSION,
        "general": {
            "theme": "dark",
            "language": "zh-CN",
        },
        # 只记录与命令默认值不同的绑定；None 表示被显式冲突替换后未绑定。
        "shortcuts": {"overrides": {}},
        "images": {
            "memory_cache_mode": "auto",
            # 自动模式使用运行时建议值，同时保留用户的手动配置。
            "memory_cache_mb": DEFAULT_MANUAL_IMAGE_CACHE_MB,
        },
        "import": {
            # 库内本体使用 ZIP、7z 或 RAR 归档，部署时解压为独立副本。
            # 非严格格式模式保留外来归档格式，目录输入按指定格式压缩。
            "format": "7z",
            "strict_format": False,
            # 0 保存自动推荐模式，运行时再根据 CPU / 内存选择线程数。
            "parallel_workers": 0,
        },
        "mods": {
            # permanent = 直接删除；recycle_bin = 优先移入系统回收站。
            "delete_method": "permanent",
            # 0 = 根据设备自动推荐；正整数 = 用户指定的同时部署数量。
            # 保存自动模式本身，避免把当前设备的推荐值固化成用户偏好。
            "deploy_concurrency": 0,
        },
        "export": {
            "parallel_workers": 0,  # 0 = 自动推荐；正整数 = 手动线程数
            "format": "zip",  # "zip" | "7z" | "rar" | "folder"
            "strict_format": False,
            "preserve_tree": True,  # 保留角色分类目录结构
            # 附带可分享元数据与手动补充图；本体原始预览图不重复进入信封。
            "include_metadata": False,
        },
        "ui": {
            "close_behavior": "exit",
            # 类别总览 / 画廊的成人级(X) Mod 预览模式：
            # "show"=正常显示 | "hide"=隐藏占位 | "blur"=模糊（与 Mod 商店 NSFW 模糊一致）
            "adult_preview_mode": "show",
            # 图片放大查看窗口的记忆尺寸（0=未记忆，用默认「合适窗口」≈屏幕 72%）。
            "image_viewer": {"width": 0, "height": 0},
            # 检测更新弹窗尺寸（逻辑像素；0 表示使用默认尺寸）。
            "mod_updates": {"width": 0, "height": 0},
            # 卡片大小滑块的记忆值（百分比）：总览/类别页 50~200、游戏页 70~150。
            # 持久化以便下次启动恢复上一次的缩放。
            "overview_card_scale": 100,
            "game_card_scale": 100,
            # 类别总览的视图模式（"grid"=卡片 / "list"=列表）记忆，下次启动恢复。
            "overview_view_mode": "grid",
            # 布局宽度按像素保存；图文分栏保存两段像素高度，并按比例恢复。
            "game_detail_width": 320,  # 游戏页右侧详情栏宽度
            "overview_preview_width": 350,  # 类别总览右侧预览栏宽度
            "overview_preview_split": [250, 150],  # 总览预览栏内 [预览图区, 文本区] 高度
            "store_detail_width": 480,  # 在线Mod 预览栏（浮窗）宽度
            "store_detail_split": [360, 240],  # 在线Mod 预览栏内 [预览图区, 文件区] 高度
            "nav_expanded_width": 160,  # 顶级菜单/导航栏展开宽度
            # 保存游戏页卡片或列表视图模式。
            "game_view_mode": "grid",
            # 列表列宽为 None 时自动适配；用户拖动后保存三列像素宽度。
            "game_list_col_widths": None,
            # 窗口尺寸为 0 时采用默认值，最大化状态单独保存。
            "window_width": 0,
            "window_height": 0,
            "window_maximized": False,
            # 单选类别的冲突确认设置：确认并勾选不再提示后，自动禁用冲突 Mod 并启用选中项。
            "suppress_character_conflict_prompt": False,
        },
        # 整窗共用背景图片，遮罩强度按明暗主题分别保存。
        "appearance": {
            "font_family": "",  # 空字符串 = 系统默认字体，不固化当前设备的字体名。
            "background": {
                "enabled": False,
                "source": "image",  # 旧配置的 enabled/path 仍代表图片背景。
                "path": "",
                "video_path": "",
                "scene_path": "",
                "engine_path": "",
                "fps": 30,
                "scene_scale": 2,
                "volume": 0,
                "scrim_dark": 45,  # 深色主题遮罩暗度 %
                "scrim_light": 30,  # 浅色主题柔光遮罩强度 %
                "blur": 60,  # 背景模糊强度 0-100（0=关）
                "fit": "cover",  # cover(填充) | contain(适应) | tile(平铺)
            },
        },
        "gamebanana": {
            "enabled": True,
            "default_sort": "best_match",
            "nsfw_policy": "blur",
            "max_concurrency": 8,
            "request_interval_ms": 200,
            # 图像磁盘缓存容量以 MB 计，0 表示不限；超限时先清理最旧文件。
            # image_cache_max_mb 为实际容量上限，image_cache_range_mb 仅决定滑块可选范围。
            "image_cache_max_mb": 512,
            "image_cache_range_mb": 4096,
            # 安装成功入库后删除 data/downloads 里的源安装包——库容器内已有一份本体，
            # 留着就是同一个 Mod 占两份磁盘。关闭则保留源包（便于重装 / 离线留档）。
            "delete_archive_after_install": True,
            # 代理配置。
            "proxy_config": {
                "active": False,  # 「代理总开关」(常规页代理配置面板)：代理是否全局可用
                "enabled": False,  # 「启用代理」(Mod 商店页)：Mod 商店是否使用代理（需 active）
                "mode": "system",  # "system" | "manual"（代理类型，在常规页配置）
                "type": "http",  # "http" | "https" | "socks5"
                "host": "127.0.0.1",
                "port": "7890",
            },
            "recent_searches": [],
            "recent_searches_max": 10,
            "character_to_search_button": True,
            "infinite_scroll_min_per_load": 10,
            "infinite_scroll_max_backfill": 5,
            "update_check": {
                # 0 保留自动推荐；正整数仅限制 Mod 更新的同时下载数量。
                "concurrency": 0,
                "enabled": True,  # 总开关
                "on_startup": True,  # 启动时在后台检查更新。
                "periodic_hours": 0,  # 0=关闭定时检查；>0 表示每 N 小时
                "min_recheck_hours": 24,  # 单个 Mod 的节流间隔
                "notify_style": "badge",  # "badge"=仅角标 | "badge_toast"=角标+状态栏
            },
        },
        # 程序自身自动更新（区别于 gamebanana.update_check 的「Mod」更新）
        "app_update": {
            "on_startup": True,  # 启动时静默检查（带 24h 节流）
            "repo": "",  # GitHub owner/repo，留空用 constants.APP_UPDATE_REPO
            "backup_before_update": True,  # 更新前备份程序文件，替换失败时用于恢复。
            "skipped_version": "",  # 用户点「跳过此版本」后记录，静默检查时不再打扰
            # 上次检查的 epoch 秒（time.time 是浮点，默认值也必须是浮点，
            # 否则会被 schema 校验当成类型不符丢弃、把节流状态清零）
            "last_check_time": 0.0,
            "download_source": "auto",  # 更新包下载源：auto=镜像优先回退；或 constants 里的镜像 key
        },
    }

    # 值域受限的键：值不在白名单 → **只把这一项**拉回默认，不动其它设置、不重建整份文件。
    # 只登记「代码按值分支」的键（写错会静默走错分支）；开放取值的键（repo / host /
    # skipped_version 等）不登记，避免把用户的合法值误判成坏值。
    _ENUMS: dict[str, tuple[str, ...]] = {
        "general.theme": ("dark", "light", "system"),
        "import.format": ("zip", "7z", "rar"),
        "mods.delete_method": ("permanent", "recycle_bin"),
        "export.format": ("zip", "7z", "rar", "folder"),
        "ui.adult_preview_mode": ("show", "hide", "blur"),
        "ui.overview_view_mode": ("grid", "list"),
        "ui.game_view_mode": ("grid", "list"),
        "appearance.background.fit": ("cover", "contain", "tile"),
        "appearance.background.source": ("off", "image", "video", "scene"),
        "gamebanana.nsfw_policy": ("show", "blur", "remove"),
        "gamebanana.default_sort": (
            "best_match",
            "newest",
            "latest",
            "most_liked",
            "most_downloaded",
            "most_viewed",
        ),
        "gamebanana.proxy_config.mode": ("system", "manual"),
        "gamebanana.proxy_config.type": ("http", "https", "socks5"),
        "gamebanana.update_check.notify_style": ("badge", "badge_toast"),
    }

    def __new__(cls) -> Config:
        """复用进程内唯一配置实例，并在首次分配时标记尚未初始化。"""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        """仅首次构造时确定设置文件路径、初始化数据并加载磁盘配置。"""
        if self._initialized:
            return
        self._initialized = True
        self._config_path = get_config_dir() / "settings.json"
        self._data: dict[str, Any] = {}
        self._load()

    # 加载与校验

    @classmethod
    def _defaults_copy(cls) -> dict[str, Any]:
        """通过 JSON 往返复制默认设置，避免嵌套对象被调用方修改。"""
        return json.loads(json.dumps(cls._DEFAULT_SETTINGS))

    @staticmethod
    def _map_file_paths(data: dict, convert: Callable[[str], str]) -> dict:
        """深复制配置后转换背景资源路径，其余字段保留原值。"""
        result = deepcopy(data)
        appearance = result.get("appearance")
        background = appearance.get("background") if isinstance(appearance, dict) else None
        if isinstance(background, dict):
            for key in ("path", "video_path", "scene_path", "engine_path"):
                if isinstance(background.get(key), str):
                    background[key] = convert(background[key])
        return result

    def _load(self) -> None:
        """读取配置文件。缺文件写完整默认；存在则校验后按原样使用（不补字段）。"""
        try:
            raw = read_json(self._config_path)
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
            self._rebuild_from_defaults(f"无法解析: {e}")
            return

        if raw is None:
            self._data = self._defaults_copy()
            self._save()
            return

        if not isinstance(raw, dict):
            self._rebuild_from_defaults(f"根节点是 {type(raw).__name__}，不是对象")
            return

        # 版本标记不同仅记录日志；缺失键使用默认值，未知键原样保留。
        version = raw.get("schema_version")
        if version != SETTINGS_SCHEMA_VERSION:
            logger.info(
                "配置文件 schema_version=%r 与本程序的 %d 不同，按「缺失即默认」照常读取",
                version,
                SETTINGS_SCHEMA_VERSION,
            )
        raw["schema_version"] = SETTINGS_SCHEMA_VERSION
        self._data = raw

        for path in self._sanitize(self._data, self._DEFAULT_SETTINGS, ""):
            logger.warning("配置项 %s 的值不合法，已回退到内置默认值（其余设置未受影响）", path)
        self._data = self._map_file_paths(self._data, resolve_stored_path)
        logger.info("配置已加载: %s", self._config_path)

    @classmethod
    def _sanitize(
        cls,
        node: dict[str, Any],
        defaults: dict[str, Any],
        prefix: str,
    ) -> list[str]:
        """校验已声明配置项的类型和值域，返回移除项的键路径。

        未知键保留。默认值为 None 时不校验，空字典和空列表仅检查容器类型。
        """
        dropped: list[str] = []
        for key, default_val in defaults.items():
            if key not in node:
                continue  # 缺键不补，读取时由默认回退处理
            path = f"{prefix}{key}"
            value = node[key]
            if default_val is None:
                continue
            if isinstance(default_val, dict):
                if not isinstance(value, dict):
                    del node[key]
                    dropped.append(path)
                elif default_val:
                    dropped.extend(cls._sanitize(value, default_val, f"{path}."))
                continue
            if not cls._value_ok(path, value, default_val):
                del node[key]
                dropped.append(path)
        return dropped

    @classmethod
    def _value_ok(cls, path: str, value: Any, default_val: Any) -> bool:
        # bool 是 int 的子类，必须先判 bool，否则 True 会被当合法整数放过
        """按默认类型、选项枚举及资源上下限验证配置值，拒绝整数位置的布尔值。"""
        if isinstance(default_val, bool):
            if not isinstance(value, bool):
                return False
        elif isinstance(default_val, float):
            # 时间戳一类的浮点键：JSON 只有一种数字类型，整数写法同样合法
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return False
        elif isinstance(default_val, int):
            if isinstance(value, bool) or not isinstance(value, int):
                return False
        elif isinstance(default_val, str):
            if not isinstance(value, str):
                return False
        elif isinstance(default_val, list):
            if not isinstance(value, list):
                return False
        if path == "general.language":
            from src.utils.language_pack import valid_language_code

            return valid_language_code(value)
        if path == "mods.deploy_concurrency":
            return 0 <= value <= MAX_DEPLOY_CONCURRENCY
        if path in ("import.parallel_workers", "export.parallel_workers"):
            return 0 <= value <= MAX_ARCHIVE_WORKERS
        if path == "gamebanana.update_check.concurrency":
            return 0 <= value <= MAX_UPDATE_CONCURRENCY
        if path == "images.memory_cache_mb":
            return MIN_IMAGE_CACHE_MB <= value <= MAX_IMAGE_CACHE_MB
        allowed = cls._allowed_values(path)
        return not (allowed and value not in allowed)

    @classmethod
    def _allowed_values(cls, path: str) -> tuple[str, ...] | None:
        """返回配置项允许的枚举值，包含当前下载镜像；无枚举限制时返回 None。"""
        if path == "images.memory_cache_mode":
            return ("auto", "manual")
        if path == "app_update.download_source":
            # 镜像 key 由 constants 定义，就地取用避免两处白名单漂移
            from src.utils.constants import APP_UPDATE_DOWNLOAD_MIRRORS

            return ("auto", *(k for k, _name, _prefix in APP_UPDATE_DOWNLOAD_MIRRORS))
        return cls._ENUMS.get(path)

    def _rebuild_from_defaults(self, reason: str) -> None:
        """备份无法解析的设置文件后恢复默认配置。

        备份失败时保留原文件，本次运行使用默认值；仅处理程序设置文件。
        """
        logger.error("配置文件无效（%s），已备份并重建默认配置: %s", reason, self._config_path)
        if backup_invalid_file(self._config_path) is None:
            logger.error("备份失败，保留原文件不覆盖；本次以默认设置运行")
            self._data = self._defaults_copy()
            return
        self._data = self._defaults_copy()
        self._save()

    def _save(self) -> bool:
        """将路径转换为存储形式后原子保存配置，写入失败时记录日志并返回 False。"""
        try:
            write_json_atomic(self._config_path, self._map_file_paths(self._data, to_stored_path))
        except OSError as e:
            logger.error("配置保存失败: %s", e)
            return False
        logger.debug("配置已保存")
        return True

    # 读写

    @staticmethod
    def _lookup(root: Any, key: str) -> Any:
        """按点号分隔的键读取嵌套字典，任一层缺失时返回专用缺失标记。"""
        node = root
        for k in key.split("."):
            if isinstance(node, dict) and k in node:
                node = node[k]
            else:
                return _MISSING
        return node

    def get(self, key: str, default: Any = None) -> Any:
        """按点分隔路径读取配置值。

        依次采用用户值、内置默认值和调用方默认值。字典及列表以副本返回，
        修改配置须通过 set 完成。例如 config.get("general.theme")。
        """
        for root in (self._data, self._DEFAULT_SETTINGS):
            value = self._lookup(root, key)
            if value is _MISSING:
                continue
            if isinstance(value, (dict, list)):
                return json.loads(json.dumps(value))
            return value
        return default

    def set(self, key: str, value: Any) -> None:
        """按点号路径更新配置值并补建字典层级，更新模式版本、解析路径后保存。"""
        keys = key.split(".")
        node = self._data
        for k in keys[:-1]:
            if k not in node or not isinstance(node[k], dict):
                node[k] = {}
            node = node[k]
        node[keys[-1]] = value
        self._data["schema_version"] = SETTINGS_SCHEMA_VERSION
        self._data = self._map_file_paths(self._data, resolve_stored_path)
        self._save()

    def get_all(self) -> dict:
        """返回顶层配置字典的浅副本，嵌套值仍与当前配置共享。"""
        return self._data.copy()

    def set_atomic(self, key: str, value: Any) -> bool:
        """单项事务写入：先写入磁盘再发布，失败不改变当前有效设置。"""
        candidate = deepcopy(self._data)
        node = candidate
        keys = key.split(".")
        for part in keys[:-1]:
            if not isinstance(node.get(part), dict):
                node[part] = {}
            node = node[part]
        node[keys[-1]] = deepcopy(value)
        candidate["schema_version"] = SETTINGS_SCHEMA_VERSION
        try:
            write_json_atomic(self._config_path, self._map_file_paths(candidate, to_stored_path))
        except OSError as exc:
            logger.error("配置项 %s 保存失败，已保持原值: %s", key, exc)
            return False
        self._data = self._map_file_paths(candidate, resolve_stored_path)
        return True

    def reset(self) -> bool:
        """恢复程序设置的默认值，成功返回 True。

        仅处理 settings.json，游戏配置由独立存储管理。先持久化再发布到内存，
        写入失败时返回 False 并保留当前状态，避免界面与磁盘配置不一致。
        """
        candidate = self._defaults_copy()
        try:
            write_json_atomic(self._config_path, candidate)
        except OSError as e:
            logger.error("恢复默认设置失败，已保持原有设置: %s", e)
            return False
        self._data = candidate
        logger.info("程序设置已重置为默认值（游戏配置不受影响）")
        return True

    def reload(self) -> None:
        """重新加载配置文件"""
        self._load()

    @classmethod
    def instance(cls) -> Config:
        """获取单例实例"""
        return cls()
