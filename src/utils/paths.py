"""路径管理工具"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
from functools import lru_cache
from pathlib import Path

from src.utils.fs import normal_path

_APP_DIR_LOCK = threading.Lock()


def is_frozen() -> bool:
    """通过 Nuitka 注入的模块标记判断是否运行于编译产物。"""
    return "__compiled__" in globals()


def get_install_dir() -> Path:
    """程序安装根目录；定位可替换的分发文件，不探测写权限或回退到用户数据目录。"""
    if is_frozen():
        compiled = globals()["__compiled__"]
        if compiled.onefile:
            return Path(compiled.containing_dir).resolve()
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent.parent


def _is_writable(directory: Path) -> bool:
    """探测目录是否可写（落地一个临时探针文件再删）。"""
    try:
        directory.mkdir(parents=True, exist_ok=True)
        # 主进程和更新子进程可能同时启动；每次使用独立探针，避免彼此占用/删除。
        with tempfile.TemporaryFile(dir=directory, prefix=".write_probe-") as probe:
            probe.write(b"ok")
        return True
    except OSError:
        return False


@lru_cache(maxsize=8)
def _frozen_app_dir(executable_directory: str, local_app_data: str) -> Path:
    """一个进程只选定一次数据根，后续临时 IO 失败不能切换到另一份数据。"""
    exe_dir = Path(executable_directory).resolve()
    if _is_writable(exe_dir):
        return exe_dir
    app_data = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
    app_dir = app_data / "d3dxSkinManage-Plus"
    app_dir.mkdir(parents=True, exist_ok=True)
    return app_dir


@lru_cache(maxsize=8)
def _development_app_dir(module_file: str) -> Path:
    """开发态也只在首次定位时访问磁盘，避免每个 Mod/预览路径都探测目录。"""
    app_dir = Path(module_file).resolve().parent.parent.parent
    app_dir.mkdir(parents=True, exist_ok=True)
    return app_dir


def get_app_dir() -> Path:
    """获取应用数据目录。

    源码运行使用项目根目录，打包运行优先使用可执行文件所在目录。
    Nuitka 单文件模式通过 containing_dir 定位外部启动器，避免写入临时解包目录。
    仅当程序目录不可写时使用本地应用数据目录。
    """
    # lru_cache 自身允许并发首次求值；锁住初始化，后续路径转换只读缓存。
    # 不在这里重复 resolve/mkdir/stat：源库轮询会为每个 Mod 的每张预览调用本函数。
    with _APP_DIR_LOCK:
        if is_frozen():
            compiled = globals()["__compiled__"]
            directory = compiled.containing_dir if compiled.onefile else os.path.dirname(sys.executable)
            return _frozen_app_dir(directory, os.environ.get("LOCALAPPDATA", ""))
        return _development_app_dir(__file__)


def get_data_dir() -> Path:
    """获取数据存储目录"""
    data_dir = get_app_dir() / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


def get_config_dir() -> Path:
    """获取配置目录"""
    config_dir = get_app_dir() / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    return config_dir


def get_cache_dir() -> Path:
    """获取缓存目录"""
    cache_dir = get_app_dir() / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def get_gb_image_cache_dir() -> Path:
    """GameBanana 缩略图/预览图的磁盘缓存目录（可再生、可随时清理）。

    固定保存在 cache/gb_images，由缓存清理功能管理。
    """
    d = get_cache_dir() / "gb_images"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_gb_download_dir() -> Path:
    """GameBanana 下载的 Mod 压缩包与 ``queue.json`` 目录（用户资产，非可丢弃缓存）。

    固定落在 ``data/downloads`` —— 归为用户数据，随程序更新保留、不被「清理缓存」误删。
    """
    d = get_data_dir() / "downloads"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_gb_api_cache_dir() -> Path:
    """GameBanana API 响应（分类树/详情/更新等 JSON）的磁盘缓存目录。

    保存在 cache/gb_api，重启后可继续使用尚未过期的接口数据。
    """
    d = get_cache_dir() / "gb_api"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_qss_icon_cache_dir() -> Path:
    """返回供 QSS 引用的主题化 SVG 缓存目录。

    优先使用程序缓存目录；路径含非 ASCII 字符时使用系统临时目录，
    避免 Qt 无法解析 QSS URL。
    """
    import tempfile

    candidate = get_cache_dir() / "qss_icons"
    target = candidate if str(candidate).isascii() else Path(tempfile.gettempdir()) / "d3dxskinmanage_qss"
    target.mkdir(parents=True, exist_ok=True)
    return target


def get_log_dir() -> Path:
    """获取日志目录"""
    log_dir = get_app_dir() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir


def get_plugins_dir() -> Path:
    """插件唯一加载目录：开发态项目根、打包态应用目录下的 plugins/。"""
    plugins_dir = get_app_dir() / "plugins"
    plugins_dir.mkdir(parents=True, exist_ok=True)
    return plugins_dir


def get_resources_dir() -> Path:
    """定位随模块分发的资源；Nuitka 单文件模式下指向解包目录。"""
    return Path(__file__).resolve().parent.parent.parent / "resources"


def to_stored_path(path: str | Path) -> str:
    """保存文件引用：程序目录内相对化，外部文件保留绝对路径。

    只用于受管文件/图片等字段；部署目录、游戏路径等明确的外部定位不得调用它。
    使用普通路径和正斜杠，不把 Windows 长路径前缀或启动工作目录写进数据。
    """
    if not path:
        return ""
    root = get_app_dir()
    candidate = Path(os.path.normpath(root / normal_path(path)))
    try:
        return candidate.relative_to(root).as_posix()
    except ValueError:
        return str(candidate)


def resolve_stored_path(path: str | Path) -> str:
    """把持久化文件引用解析为运行时路径；相对路径恒以程序数据根目录为基准。"""
    if not path:
        return ""
    return os.path.normpath(get_app_dir() / normal_path(path))


def is_archive(path: str | Path) -> bool:
    """判断文件是否为压缩包"""
    from src.utils.constants import ARCHIVE_EXTENSIONS

    return Path(path).suffix.lower() in ARCHIVE_EXTENSIONS


def is_image(path: str | Path) -> bool:
    """判断文件是否为图片"""
    from src.utils.constants import IMAGE_EXTENSIONS

    return Path(path).suffix.lower() in IMAGE_EXTENSIONS
