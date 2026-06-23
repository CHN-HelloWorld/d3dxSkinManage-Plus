"""提供配置文件的原子读写与无效文件备份。

程序设置和游戏配置共用文件操作，业务键、默认值及恢复范围由各存储服务管理。
写入时先生成同目录暂存文件再原子替换；无法解析的文件通过重命名保留原文。
文件操作异常向调用方传播，由业务层决定如何恢复。
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from src.utils.logger import logger


def write_json_atomic(path: Path, data: Any) -> None:
    """把 data 以 UTF-8 JSON 写入 path（同目录临时文件 + 原子替换）。

    临时文件必须与目标**同目录**：跨卷的 `os.replace` 不是原子操作，临时目录常在别的盘。
    失败时清理临时文件并向上抛，目标文件保持原样。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def read_json(path: Path) -> Any | None:
    """读取 JSON；文件不存在返回 None。解析失败/编码错误向上抛。"""
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def backup_invalid_file(path: Path, tag: str = "invalid") -> Path | None:
    """重命名无效文件以保留原文，返回备份路径。

    同一秒内的重复备份追加序号；备份失败时返回 None，保留原文件。
    """
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = path.with_name(f"{path.name}.{tag}-{stamp}")
    seq = 1
    while target.exists():
        target = path.with_name(f"{path.name}.{tag}-{stamp}-{seq}")
        seq += 1
    try:
        os.replace(path, target)
    except OSError as e:
        logger.error("备份无效配置文件失败: %s", e)
        return None
    logger.info("原配置文件已备份为: %s", target)
    return target
