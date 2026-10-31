"""准备 GitHub 云端发行版本、独立视频资源和可供主程序识别的发布包。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[2]
PROJECT_NAME = "d3dxSkinManage-Plus"
SHA256_PATTERN = re.compile(r"SHA256[:\s]+([a-fA-F0-9]{64})")


def file_sha256(path: Path) -> str:
    """分块计算文件摘要，避免把完整发行包读入内存。"""
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, value: dict) -> None:
    """使用 UTF-8 和固定换行写入供后续工作流步骤读取的元数据。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")


def replace_once(source: str, pattern: str, replacement: str, label: str) -> str:
    """仅更新唯一匹配的版本字段，源码结构变化时停止而不猜测替换位置。"""
    result, count = re.subn(pattern, lambda _match: replacement, source, flags=re.MULTILINE)
    if count != 1:
        raise ValueError(f"{label} 应恰好出现一次，实际找到 {count} 处。")
    return result


def prepare_release(root: Path, inputs: dict, repository: str) -> dict:
    """校验手动输入，再同步写入主程序、包元数据和原生更新器的发行版本。"""
    mode = inputs.get("mode", "publish")
    if mode not in {"build", "publish"}:
        raise ValueError("mode 只能为 build 或 publish。")
    version = inputs.get("version", "").strip()
    if version.startswith(("v", "V")):
        version = version[1:]
    if not re.fullmatch(r"(?:0|[1-9][0-9]*)(?:\.(?:0|[1-9][0-9]*)){2,3}", version):
        raise ValueError("版本号应为 1.2.3 或 1.2.3.4，可加 v 前缀，不支持预发布后缀或前导零。")
    parts = [int(part) for part in version.split(".")]
    if any(part > 65535 for part in parts):
        raise ValueError("Windows 版本号的每一段必须在 0–65535 之间。")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("更新仓库必须使用 owner/repo 格式。")

    title = inputs.get("release_title", "").strip()
    notes = inputs.get("release_notes", "").replace("\r\n", "\n")
    # 网页输入框可用字面的 \n 换行；CLI 传入的实际多行 Markdown 保留原样。
    if "\n" not in notes:
        notes = notes.replace("\\n", "\n")
    if mode == "publish" and (not title or not notes.strip()):
        raise ValueError("发布时必须填写 release_title（标题）和 release_notes（正文）。")
    if "\n" in title or "\r" in title:
        raise ValueError("发布标题应为单行文本。")
    if SHA256_PATTERN.search(notes):
        raise ValueError("正文中不要填写 SHA256 校验值；工作流会根据本次 ZIP 自动生成。")

    windows_parts = parts + [0] * (4 - len(parts))
    windows_version = ".".join(map(str, windows_parts))
    changes = {
        "src/utils/constants.py": (
            (r'^APP_VERSION[ \t]*=[ \t]*"[^"]*"', f'APP_VERSION = "{version}"'),
            (r'^APP_UPDATE_REPO[ \t]*=[ \t]*"[^"]*"', f'APP_UPDATE_REPO = "{repository}"'),
        ),
        "pyproject.toml": ((r'^version[ \t]*=[ \t]*"[^"]*"', f'version = "{version}"'),),
        "updater/updater.rc": (
            (r"^[ \t]*FILEVERSION[ \t]+[0-9, ]+", " FILEVERSION " + ",".join(map(str, windows_parts))),
            (r"^[ \t]*PRODUCTVERSION[ \t]+[0-9, ]+", " PRODUCTVERSION " + ",".join(map(str, windows_parts))),
            (r'VALUE "FileVersion", "[^"\n]*"', f'VALUE "FileVersion", "{windows_version}\\0"'),
            (r'VALUE "ProductVersion", "[^"\n]*"', f'VALUE "ProductVersion", "{windows_version}\\0"'),
        ),
        "updater/updater.manifest": (
            (r'<assemblyIdentity version="[^"]*"', f'<assemblyIdentity version="{windows_version}"'),
        ),
    }
    prepared = {}
    for relative, replacements in changes.items():
        path = root / relative
        source = path.read_text(encoding="utf-8")
        for pattern, replacement in replacements:
            source = replace_once(source, pattern, replacement, relative)
        prepared[path] = source
    for path, source in prepared.items():
        path.write_text(source, encoding="utf-8", newline="\n")

    metadata = {
        "mode": mode,
        "version": version,
        "tag": f"v{version}",
        "title": title or f"v{version}",
        "notes": notes,
        "repository": repository,
        "asset_name": f"{PROJECT_NAME}-v{version}-win64.zip",
    }
    write_json(root / "build/release-inputs.json", metadata)
    return metadata


def media_manifest(root: Path) -> tuple[dict, Path]:
    """读取固定媒体清单，并确保恢复目标位于仓库的短片资源目录内。"""
    directory = (root / "resources/about/story").resolve()
    manifest = json.loads((directory / "media-manifest.json").read_text(encoding="utf-8"))
    relative = PurePosixPath(manifest["archive_path"])
    if relative.is_absolute() or ".." in relative.parts or "\\" in str(relative) or ":" in str(relative):
        raise ValueError("媒体清单中的归档路径无效。")
    target = root.joinpath(*relative.parts).resolve()
    if not target.is_relative_to(directory) or target == directory:
        raise ValueError("媒体恢复目标必须位于 resources/about/story 内。")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+\.zip", manifest["archive"]):
        raise ValueError("媒体资源包名称无效。")
    if not re.fullmatch(r"[a-fA-F0-9]{64}", manifest["sha256"]) or manifest["bytes"] <= 0:
        raise ValueError("媒体清单必须包含有效的 SHA256 和文件字节数。")
    return manifest, target


def pack_media(root: Path) -> Path:
    """验证本地视频后制作仅包含该视频的独立资源 ZIP，供首次配置云端构建使用。"""
    manifest, video = media_manifest(root)
    if video.stat().st_size != manifest["bytes"] or file_sha256(video) != manifest["sha256"].lower():
        raise ValueError("本地视频与 media-manifest.json 的大小或 SHA256 不符。")
    archive = root / "build" / manifest["archive"]
    archive.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as package:
        package.write(video, manifest["archive_path"])
    return archive


def restore_media(root: Path, archive: Path) -> Path:
    """只读取清单指定的视频，大小与摘要一致后再写入，忽略资源包内的其他条目。"""
    manifest, target = media_manifest(root)
    with zipfile.ZipFile(archive) as package:
        matches = [item for item in package.infolist() if item.filename == manifest["archive_path"]]
        if len(matches) != 1 or matches[0].file_size != manifest["bytes"]:
            raise ValueError("资源包缺少唯一的视频文件，或文件大小与清单不符。")
        content = package.read(matches[0])
    if len(content) != manifest["bytes"] or hashlib.sha256(content).hexdigest() != manifest["sha256"].lower():
        raise ValueError("视频资源的 SHA256 与 media-manifest.json 不符。")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    return target


def create_bundle(root: Path) -> Path:
    """核验发行 ZIP，生成正文和摘要文件，并整理供构建下载及发布任务共用的附件目录。"""
    build = root / "build"
    metadata = json.loads((build / "release-inputs.json").read_text(encoding="utf-8"))
    archive = build / metadata["asset_name"]
    with zipfile.ZipFile(archive) as package:
        prefix = archive.stem + "/"
        required = (f"{PROJECT_NAME}.exe", "Updater/updater.exe", "files.manifest")
        missing = [name for name in required if prefix + name not in package.namelist()]
        if missing:
            raise ValueError("发行 ZIP 缺少必需文件：" + ", ".join(missing))
        damaged = package.testzip()
        if damaged:
            raise ValueError(f"发行 ZIP 校验失败：{damaged}")

    digest = file_sha256(archive)
    body = metadata["notes"].rstrip() + f"\n\n<!-- SHA256: {digest} -->\n"
    metadata.update(
        body=body,
        sha256=digest,
        asset_size=archive.stat().st_size,
        checksum_name=archive.name + ".sha256",
    )
    bundle = build / "release"
    bundle.mkdir(parents=True, exist_ok=True)
    shutil.copy2(archive, bundle / archive.name)
    (bundle / "release-notes.md").write_text(body, encoding="utf-8", newline="\n")
    (bundle / metadata["checksum_name"]).write_text(f"{digest}  {archive.name}\n", encoding="utf-8", newline="\n")
    write_json(bundle / "release.json", metadata)
    return bundle


def main() -> int:
    """提供工作流和维护者使用的版本准备、媒体制作与恢复、附件整理命令。"""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare", help="从 workflow_dispatch 事件准备构建版本")
    prepare.add_argument("--event", type=Path, default=os.environ.get("GITHUB_EVENT_PATH"))
    prepare.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", ""))
    commands.add_parser("pack-media", help="校验本地视频并生成独立资源 ZIP")
    media = commands.add_parser("restore-media", help="校验并恢复下载的视频资源 ZIP")
    media.add_argument("--archive", type=Path, required=True)
    commands.add_parser("bundle", help="核验发行 ZIP 并生成发布附件")
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            if not args.event:
                raise ValueError("需要 GITHUB_EVENT_PATH 或 --event 指定手动触发事件。")
            event = json.loads(args.event.read_text(encoding="utf-8"))
            metadata = prepare_release(ROOT, event["inputs"], args.repository)
            if output := os.environ.get("GITHUB_OUTPUT"):
                with Path(output).open("a", encoding="utf-8", newline="\n") as target:
                    for name in ("version", "tag", "asset_name"):
                        target.write(f"{name}={metadata[name]}\n")
            print(f"已准备 {metadata['tag']}，更新来源：{metadata['repository']}")
        elif args.command == "pack-media":
            print(pack_media(ROOT))
        elif args.command == "restore-media":
            print(restore_media(ROOT, args.archive))
        else:
            print(create_bundle(ROOT))
    except (OSError, ValueError, KeyError, zipfile.BadZipFile) as error:
        print(f"发行准备失败：{error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
