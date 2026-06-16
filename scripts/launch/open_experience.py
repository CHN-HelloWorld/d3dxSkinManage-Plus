"""使用当前项目源码打开指定的持久大库环境，退出后保留其中的数据。"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]


def main(argv: list[str] | None = None) -> int:
    """在导入应用入口前绑定独立数据根目录，并返回应用退出码。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, required=True, help="已有大库环境的根目录，包含独立配置和数据")
    parser.add_argument("--no-elevate", action="store_true", help="跳过应用的管理员权限检查")
    args = parser.parse_args(argv)
    data_root = args.data_root.expanduser().resolve()
    if not data_root.is_dir():
        parser.error("--data-root 必须指向已有目录")
    if not (PROJECT / "src/core/library_schema.py").is_file():
        raise RuntimeError("当前项目源码不存在，无法打开大库环境")

    # 提权会保留切换后的工作目录，因此脚本和数据参数必须使用绝对路径。
    sys.argv = [str(Path(__file__).resolve()), "--data-root", str(data_root)]
    if args.no_elevate:
        sys.argv.append("--no-elevate")

    # 只读资源从项目源码加载，可写配置和数据使用独立目录。
    sys.path.insert(0, str(PROJECT))
    os.chdir(data_root)
    from src.utils import paths

    paths.get_app_dir = lambda: data_root

    from run_app import main as run_application

    return run_application()


if __name__ == "__main__":
    raise SystemExit(main())
