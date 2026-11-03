"""统一执行版本同步、原生更新器编译、主程序打包和发行附件校验。"""

from __future__ import annotations

import argparse
import ast
import os
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.release.github_release import (  # noqa: E402
    create_bundle,
    normalize_version,
    prepare_release,
    read_workflow_inputs,
    release_metadata,
    write_workflow_outputs,
)


def source_defaults(root: Path) -> tuple[str, str]:
    """直接读取源码常量，避免版本同步后使用解释器已缓存的旧值。"""
    tree = ast.parse((root / "src/utils/constants.py").read_text(encoding="utf-8"))
    values = {
        node.targets[0].id: ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id in {"APP_VERSION", "APP_UPDATE_REPO"}
    }
    return values["APP_VERSION"], values["APP_UPDATE_REPO"]


def prompt_version(current: str) -> str:
    """提示输入本次版本号；空输入保留当前版本，无效输入不进入文件写入阶段。"""
    print(f"当前源码版本：{current}")
    while True:
        entered = input(f"本次版本号 [{current}]：").strip() or current
        try:
            return normalize_version(entered)
        except ValueError as error:
            print(f"[X] {error}")


def build_commands(root: Path, nuitka_args: list[str]) -> list[list[str]]:
    """通过独立解释器依次编译更新器和主程序，使编译读取已同步的版本。"""
    python = [sys.executable, "-X", "utf8"]
    main = [*python, str(root / "scripts/release/build.py"), "--with-updater", "--zip"]
    if nuitka_args:
        main.extend(["--", *nuitka_args])
    return [
        [*python, str(root / "updater/tools/build.py"), "--updater-only"],
        main,
    ]


def main(argv: list[str] | None = None) -> int:
    """从本地参数或工作流事件读取版本，并按顺序完成发行准备与构建。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", help="本次版本号，省略时使用源码中的当前版本")
    parser.add_argument("--repository", help="更新来源 owner/repo，默认保留源码中的更新仓库")
    parser.add_argument("--title", default="", help="本地发行说明标题，默认 v版本号")
    parser.add_argument("--notes-file", type=Path, help="本地发行说明的 UTF-8 Markdown 文件")
    parser.add_argument("--interactive", action="store_true", help="交互输入版本号，供构建菜单使用")
    parser.add_argument("--prepare-only", action="store_true", help="只同步版本和发行元数据，不执行编译")
    parser.add_argument("--from-workflow", action="store_true", help="从 GitHub 手动触发事件读取版本与发布说明")
    parser.add_argument("--dry-run", action="store_true", help="只校验输入并显示步骤，不修改文件或运行构建")
    parser.add_argument("nuitka_args", nargs=argparse.REMAINDER, metavar="-- ARGS", help="透传给 Nuitka 的编译选项")
    args = parser.parse_args(argv)
    if args.from_workflow and (
        args.version is not None
        or args.repository is not None
        or args.title
        or args.notes_file is not None
        or args.interactive
    ):
        parser.error("--from-workflow 不能与本地版本、仓库、标题、正文或交互输入参数同时使用。")
    if args.interactive and args.version is not None:
        parser.error("--interactive 和 --version 请选择其中一种版本输入方式。")
    nuitka_args = args.nuitka_args
    if nuitka_args[:1] == ["--"]:
        nuitka_args = nuitka_args[1:]
    if args.prepare_only and nuitka_args:
        parser.error("--prepare-only 不执行编译，不能传入 Nuitka 选项。")

    try:
        if args.from_workflow:
            event = os.environ.get("GITHUB_EVENT_PATH")
            inputs = read_workflow_inputs(Path(event) if event else None)
            repository = os.environ.get("GITHUB_REPOSITORY", "")
        else:
            current, repository = source_defaults(ROOT)
            version = prompt_version(current) if args.interactive else args.version
            inputs = {
                "mode": "build",
                "version": current if version is None else version,
                "release_title": args.title,
                "release_notes": args.notes_file.read_text(encoding="utf-8-sig") if args.notes_file else "",
            }
            if args.repository is not None:
                repository = args.repository
        metadata = release_metadata(inputs, repository)
        commands = build_commands(ROOT, nuitka_args)
        if args.dry_run:
            print(f"计划同步版本：{metadata['tag']}，更新来源：{repository}")
            if not args.prepare_only:
                for command in commands:
                    print(subprocess.list2cmdline(command))
                print(f"校验 ZIP 并生成 SHA256：{ROOT / 'build/release' / metadata['asset_name']}")
            return 0

        metadata = prepare_release(ROOT, inputs, repository)
        if args.from_workflow:
            write_workflow_outputs(metadata)
        print(f"已同步版本：{metadata['tag']}，更新来源：{repository}", flush=True)
        if args.prepare_only:
            return 0
        for step, command in zip(("构建原生更新器", "构建主程序和发行 ZIP"), commands):
            print(f"\n== {step} ...", flush=True)
            subprocess.run(command, cwd=ROOT, check=True)
        bundle = create_bundle(ROOT)
        print(f"\n[OK] 发行包和 SHA256 已生成：{bundle}")
    except subprocess.CalledProcessError as error:
        print(f"发行构建失败（退出码 {error.returncode}），已停止后续步骤。", file=sys.stderr)
        return error.returncode
    except (OSError, ValueError, KeyError, zipfile.BadZipFile) as error:
        print(f"发行准备失败：{error}", file=sys.stderr)
        return 1
    except (EOFError, KeyboardInterrupt):
        print("\n已取消发行操作。", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
