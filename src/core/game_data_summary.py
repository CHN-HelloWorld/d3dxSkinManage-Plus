"""为游戏删除确认提供可取消的只读数据清单。"""

from __future__ import annotations

import os
import sqlite3
import stat
from contextlib import closing
from pathlib import Path
from time import monotonic

from src.utils.language_pack import N_


class GameDataScanCancelled(Exception):
    """用户在确认删除前取消清单采集。"""


def describe_game_data(db_path, game_id, root, preset_count, *, cancelled=None, progress=None):
    """只读统计游戏索引及资料库文件容量，并按阶段报告进度。

    目录扫描跳过链接与重解析点；取消请求抛出 GameDataScanCancelled，容量统计忽略扫描后已删除的文件。"""

    def checkpoint():
        """检查取消回调，收到取消请求时立即中止本次数据统计。"""
        if cancelled and cancelled():
            raise GameDataScanCancelled()

    last_update = 0.0

    def report(phase, done, total, size, *, force=False):
        """按时间间隔或强制标记发送阶段进度，并在每次调用后检查取消请求。"""
        nonlocal last_update
        now = monotonic()
        if progress and (force or now - last_update >= 0.05):
            progress(phase, done, total, size)
            last_update = now
        checkpoint()

    checkpoint()
    report(N_("读取记录"), 0, 0, 0, force=True)
    # 工作线程使用自己的数据库连接，避免访问界面连接或构造并解析全部 Mod 路径。
    with closing(sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        try:
            conn.set_progress_handler(lambda: int(bool(cancelled and cancelled())), 1000)
            mods, deployed = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(d.actual_path <> ''), 0) FROM mods m "
                "LEFT JOIN deployment_records d ON d.mod_id=m.mod_id WHERE m.game_id=?",
                (game_id,),
            ).fetchone()
            characters = conn.execute("SELECT COUNT(*) FROM nodes WHERE game_id=?", (game_id,)).fetchone()[0]
        except sqlite3.OperationalError:
            checkpoint()
            raise
        finally:
            conn.set_progress_handler(None, 0)
    info = dict(
        mods=mods, characters=characters, deployed=deployed, presets=preset_count, library_dir="", library_size=0
    )
    checkpoint()
    if root is None or not root.is_dir():
        report(N_("统计容量"), 0, 0, 0, force=True)
        return info
    info["library_dir"] = str(root)
    files = []
    directories = [root]
    scanned_directories = 0
    report(N_("扫描文件"), 0, 0, 0, force=True)
    while directories:
        checkpoint()
        directory = directories.pop()
        scanned_directories += 1
        with os.scandir(directory) as entries:
            for entry in entries:
                checkpoint()
                first_file = False
                if entry.is_dir(follow_symlinks=False):
                    attrs = getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0)
                    if not attrs & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                        directories.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    files.append(entry.path)
                    first_file = len(files) == 1
                report(N_("扫描文件"), len(files), scanned_directories, 0, force=first_file)
    total_size = 0
    report(N_("统计容量"), 0, len(files), 0, force=True)
    for done, path in enumerate(files, 1):
        checkpoint()
        try:
            total_size += os.path.getsize(path)
        except FileNotFoundError:
            # 已被其他操作删除的文件不计入磁盘占用。
            pass
        report(N_("统计容量"), done, len(files), total_size, force=done == 1 or done == len(files))
    info["library_size"] = total_size
    return info
