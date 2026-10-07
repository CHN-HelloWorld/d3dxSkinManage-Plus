"""校验插件目录并按显式文件清单生成可复现发布包。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.plugins.runtime.manifest import ManifestError
from src.plugins.runtime.packaging import pack_plugin
from src.plugins.runtime.validation import validate_package


def main() -> int:
    """解析校验或打包命令，输出便于开发者或自动化工具读取的结果。"""
    parser = argparse.ArgumentParser(description="插件开发校验与打包")
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("check", "pack"):
        subcommand = commands.add_parser(command)
        subcommand.add_argument("directory", type=Path)
        subcommand.add_argument("--json", action="store_true", help="输出 JSON 结果")
        if command == "pack":
            subcommand.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        manifest = validate_package(args.directory)
        result = {"status": "ok", "id": manifest.id, "version": manifest.version, "apiVersion": manifest.api_version}
        if args.command == "pack":
            result["output"] = str(pack_plugin(args.directory, args.output))
    except (ManifestError, OSError, ValueError) as exc:
        result = {"status": "error", "error": str(exc)}
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    elif result["status"] == "error":
        print("插件检查失败：" + result["error"])
    else:
        print(f"插件检查通过：{result['id']} v{result['version']}")
        if "output" in result:
            print("已生成：" + result["output"])
    return 0 if result["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
