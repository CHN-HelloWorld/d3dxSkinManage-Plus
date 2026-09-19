"""准备程序更新所需的磁盘空间、解压目录、备份和更新器参数。

主程序退出前完成准备；文件替换、清理和失败回滚由独立 updater.exe 执行。
"""

from __future__ import annotations

import os
import shutil
import zipfile
from pathlib import Path

from src.services.app_update.retirement import find_or_restore_updater
from src.utils.constants import UPDATER_SUBDIR
from src.utils.i18n import t as tr
from src.utils.logger import logger
from src.utils.paths import get_app_dir

# 便携模式下程序数据与运行产物全部落在程序目录内（见 paths.get_app_dir）。
# 更新时保留用户数据和运行产物，范围与原生更新器保持一致。
PRESERVE_FILES = [
    "data/",  # Mod 库、索引、预览图、缩略图
    "config/",  # 用户设置、游戏配置与语言包
    "cache/",  # 各类缓存
    "logs/",  # 日志
    "plugins/",  # 用户插件
    f"{UPDATER_SUBDIR}/",  # 通用同步保护此目录；原生更新器单独事务替换 updater.exe
]

# 备份只包含可回滚的程序文件，跳过用户数据和编译产物。
_BACKUP_SKIP = {"data", "config", "cache", "logs", "plugins", UPDATER_SUBDIR, "__pycache__"}


class AppUpdateInstaller:
    """程序更新安装器。"""

    def __init__(self, update_zip_path: str | Path, install_dir: str | Path | None = None) -> None:
        """记录更新包和安装目录，并派生更新器缓存中的解压及备份路径，不立即改动文件。"""
        self.update_zip_path = Path(update_zip_path)
        self.install_dir = Path(install_dir) if install_dir else get_app_dir()

        self.cache_dir = self.install_dir / UPDATER_SUBDIR / "Cache"
        self.extract_dir = self.cache_dir / "extract"
        self.backup_dir = self.cache_dir / "backup"

    def check_disk_space(self) -> tuple[bool, str]:
        """需要约 2 倍包大小（解压 + 备份）的可用空间。"""
        try:
            zip_size = self.update_zip_path.stat().st_size
            required = zip_size * 2
            free = shutil.disk_usage(self.install_dir).free
            req_mb, free_mb = required / 1024 / 1024, free / 1024 / 1024
            if free >= required:
                return True, tr(
                    "磁盘空间充足（需 {req_mb:.1f}MB / 可用 {free_mb:.1f}MB）", req_mb=req_mb, free_mb=free_mb
                )
            return False, tr(
                "磁盘空间不足：需至少 {req_mb:.1f}MB，当前可用 {free_mb:.1f}MB", req_mb=req_mb, free_mb=free_mb
            )
        except OSError as e:
            return False, tr("检查磁盘空间失败: {e}", e=e)

    def extract_update(self) -> bool:
        """解压更新包到 Cache/extract/；若整包套了单层顶层目录则自动展平。"""
        try:
            if self.extract_dir.exists():
                shutil.rmtree(self.extract_dir)
            self.extract_dir.mkdir(parents=True, exist_ok=True)

            with zipfile.ZipFile(self.update_zip_path, "r") as zf:
                infos = zf.infolist()
                # 修正非 UTF-8 标志位的中文条目名（Windows 工具常用 GBK 不置标志位）
                for info in infos:
                    if not (info.flag_bits & 0x800):
                        try:
                            info.filename = info.filename.encode("cp437").decode("gbk")
                        except (UnicodeDecodeError, UnicodeEncodeError):
                            pass
                names = [i.filename for i in infos]
                for info in infos:
                    zf.extract(info, self.extract_dir)

            self._flatten_single_top_dir(names)

            count = sum(1 for p in self.extract_dir.rglob("*") if p.is_file())
            logger.info("更新包解压完成：%d 个文件 -> %s", count, self.extract_dir)
            return True
        except zipfile.BadZipFile:
            logger.error("更新包损坏或非 ZIP 格式")
            return False
        except Exception as e:
            logger.error("解压更新包失败: %s", e)
            return False

    def _flatten_single_top_dir(self, names: list[str]) -> None:
        """所有条目都在同一顶层目录下时，把内容上提到 extract 根。"""
        top_dirs = {n.split("/")[0] for n in names if "/" in n}
        if len(top_dirs) != 1:
            return
        top = next(iter(top_dirs))
        if not all(n == top + "/" or n.startswith(top + "/") for n in names):
            return
        nested = self.extract_dir / top
        if not nested.is_dir():
            return
        temp = self.extract_dir.parent / "extract_temp"
        if temp.exists():
            shutil.rmtree(temp)
        nested.rename(temp)
        shutil.rmtree(self.extract_dir)
        temp.rename(self.extract_dir)
        logger.info("更新包含单层顶层目录 %s/，已展平到解压根", top)

    def backup_current_version(self) -> bool:
        """备份程序本体到 Cache/backup/（用户数据不纳入）。更新器失败时据此回滚。"""
        try:
            if self.backup_dir.exists():
                shutil.rmtree(self.backup_dir)
            self.backup_dir.mkdir(parents=True, exist_ok=True)

            n = 0
            for item in self.install_dir.iterdir():
                if item.name in _BACKUP_SKIP:
                    continue
                dest = self.backup_dir / item.name
                try:
                    if item.is_file():
                        shutil.copy2(item, dest)
                    elif item.is_dir():
                        shutil.copytree(item, dest)
                    n += 1
                except OSError as e:
                    logger.warning("备份 %s 失败: %s", item.name, e)
            logger.info("已备份当前版本：%d 个项目 -> %s", n, self.backup_dir)
            return True
        except Exception as e:
            logger.error("备份当前版本失败: %s", e)
            return False

    def locate_updater(self) -> Path | None:
        """定位 updater.exe（安装目录的 Updater/ 子目录内）。"""
        return find_or_restore_updater(self.install_dir)

    def prepare_for_updater(self) -> dict:
        """组装传给 updater.exe 的参数。"""
        return {
            "pid": os.getpid(),
            "extract_dir": str(self.extract_dir),
            "install_dir": str(self.install_dir),
            "preserve_files": list(PRESERVE_FILES),
        }

    def cleanup_extract(self) -> None:
        """移除本安装器的解压暂存目录，清理失败只记录日志，不删除备份或下载包。"""
        try:
            if self.extract_dir.exists():
                shutil.rmtree(self.extract_dir)
        except OSError as e:
            logger.warning("清理解压目录失败: %s", e)
