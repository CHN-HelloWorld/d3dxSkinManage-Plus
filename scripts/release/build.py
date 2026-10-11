#!/usr/bin/env python3
"""使用当前 Python 环境中的 Nuitka 和运行依赖构建 Windows 程序。

默认生成可分发目录，--onefile 生成单文件主程序，--with-updater 附带原生更新器。
语言包、插件、教程、项目短片及许可声明作为发行内容复制，产物统一放入项目 build 目录。
--install-deps 安装 pyproject.toml 声明的构建依赖，--check 检查环境，
--dry-run 输出编译命令，--clean 清理构建产物。完整用法见 docs/构建指南.md。
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

# constants.py 和原文标记仅依赖标准库，安全引入用于填充 exe 元数据
from src.utils.constants import (  # noqa: E402
    APP_NAME,
    APP_ORGANIZATION,
    APP_VERSION,
    UPDATER_EXE_NAME,
    UPDATER_SUBDIR,
)
from src.utils.project_story import load_project_story  # noqa: E402

# Windows 控制台常是 GBK：放宽编码错误处理，避免个别符号触发 UnicodeEncodeError 崩溃
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")  # type: ignore[union-attr]
    except Exception:
        pass

ENTRY = ROOT / "run_app.py"
RESOURCES = ROOT / "resources"
PROJECT_STORY = RESOURCES / "about" / "story"
LANGUAGES = ROOT / "config" / "languages"
SHIPPED_LANGUAGE_FILES = ("README.md", "schema.json", "zh-CN/strings.json", "en-US/strings.json")
LANGUAGE_ARCHIVE_PATHS = frozenset(f"config/languages/{name}" for name in SHIPPED_LANGUAGE_FILES)
PLUGINS = ROOT / "plugins"
SHIPPED_PLUGIN_IDS = ("example.mod-inventory", "example.preset-audit", "example.preset-cycle")
_PLUGIN_COMMON_FILES = ("plugin.json", "entry.py", "languages/en-US/strings.json", "README.md")
SHIPPED_PLUGIN_FILES = {
    "example.mod-inventory": (*_PLUGIN_COMMON_FILES, "inventory.py"),
    "example.preset-audit": _PLUGIN_COMMON_FILES,
    "example.preset-cycle": _PLUGIN_COMMON_FILES,
}
PLUGIN_ARCHIVE_PATHS = frozenset(
    f"plugins/{plugin_id}/{relative}"
    for plugin_id in SHIPPED_PLUGIN_IDS
    for relative in SHIPPED_PLUGIN_FILES[plugin_id]
)
SHIPPED_DOCUMENT_FILES = (
    "d3dxSkinManage-Plus 使用教程.html",
    "d3dxSkinManage-Plus User Guide.html",
    "d3dxSkinManage-Plus 插件开发指南.html",
    "d3dxSkinManage-Plus Plugin Development Guide.html",
)
DOCUMENT_ARCHIVE_PATHS = frozenset(f"docs/{name}" for name in SHIPPED_DOCUMENT_FILES)
# 部分 MSVC 工具按系统码页解析响应文件，含非 ASCII 的路径可能无法链接。
# 此时在同盘的 ASCII 暂存目录编译，完成后将产物移动到项目 build 目录。
PROJECT_BUILD = ROOT / "build"  # 成品最终落点
if str(PROJECT_BUILD).isascii():
    STAGING_ROOT = PROJECT_BUILD  # 项目路径已是 ASCII → 直接项目内编译
else:
    # 按仓库路径区分暂存目录，避免不同副本覆盖或清理彼此的编译缓存。
    _staging_tag = hashlib.sha1(str(ROOT).encode("utf-8")).hexdigest()[:8]
    STAGING_ROOT = Path(ROOT.anchor) / f"d3dxSkinManage-Plus-build-{_staging_tag}"
STAGING_DIR = STAGING_ROOT / "nuitka"  # 主程序编译暂存
OUTPUT_DIR = STAGING_DIR  # Nuitka --output-dir 指向暂存目录
EXE_NAME = "d3dxSkinManage-Plus.exe"
DIST_NAME = "d3dxSkinManage-Plus"  # standalone 成品文件夹名（项目 build 内）
NATIVE_UPDATER_EXE = ROOT / "updater" / "bin" / "x64" / "Release" / UPDATER_EXE_NAME
LEGAL_NOTICES = ("LICENSE", "THIRD_PARTY_NOTICES.md")
ICON = RESOURCES / "app.ico"  # 存在则用作程序图标（可选）
# 排除运行生成的用户数据，仅通过白名单加入默认配置和内置插件。
RUNTIME_ARTIFACTS = ("data", "config", "cache", "logs", "plugins")
DEFAULT_INDEX_URL = "https://pypi.org/simple"

# 运行期必须能 import 的包：(import 名, pip 名)
REQUIRED = [
    ("PySide6", "PySide6"),
    ("PySide6.QtMultimedia", "PySide6"),
    ("shiboken6", "shiboken6"),
    ("windows_capture", "windows-capture"),
    ("py7zr", "py7zr"),
    ("httpx", "httpx"),
    ("PIL", "Pillow"),
    ("pypinyin", "pypinyin"),
    ("bleach", "bleach"),
    ("tinycss2", "tinycss2"),  # 保留富文本描述中的内联颜色和字体样式
    ("tenacity", "tenacity"),
    ("rapidfuzz", "rapidfuzz"),
    ("watchdog", "watchdog"),
    ("certifi", "certifi"),  # httpx 的 TLS 根证书，缺失会导致目标机 HTTPS 失败
    ("unrar", "unrar2-cffi"),  # RAR 解压首选后端（自带引擎，无需系统装 WinRAR）
    ("rarfile", "rarfile"),  # RAR 回退后端（依赖系统外部工具）
]


def have(mod: str) -> bool:
    """该 import 名是否可被当前解释器解析到。"""
    try:
        return importlib.util.find_spec(mod) is not None
    except (ImportError, ValueError):
        return False


def nuitka_version() -> tuple[int, ...]:
    """返回当前解释器里 Nuitka 的版本元组，取不到返回空元组。"""
    try:
        # 直接读取包元数据，避免 --version 重启解释器或探测编译工具，导致 Windows 子进程超时。
        text = importlib.metadata.version("Nuitka")
    except importlib.metadata.PackageNotFoundError:
        return ()
    if not text:
        return ()
    m = re.match(r"(\d+)\.(\d+)(?:\.(\d+))?", text)
    if not m:
        return ()
    return tuple(int(g) for g in m.groups(default="0"))


def build_command(args: argparse.Namespace) -> list[str]:
    """根据构建选项生成 Nuitka 参数列表，显式声明运行依赖和资源，并保留调用方追加参数。"""
    # Nuitka 读取 .pth 文件时需按 UTF-8 解析含中文的项目路径。
    cmd = [sys.executable, "-X", "utf8", "-m", "nuitka"]
    cmd.append("--onefile" if args.onefile else "--standalone")
    cmd += [
        "--enable-plugin=pyside6",  # Qt 绑定及 platforms/imageformats/iconengines 等插件
        "--include-qt-plugins=sensible,multimedia",  # 原生视频解码与音频后端
        "--include-package=windows_capture",  # 场景捕获的 Rust 扩展及 Python 包装
        f"--user-package-configuration-file={ROOT / 'scripts' / 'release' / 'nuitka-package.config.yml'}",
        "--nofollow-import-to=cv2",  # 捕获库的图片保存已改用 Pillow，不携带 OpenCV
        f"--report={PROJECT_BUILD / 'nuitka-report.xml'}",  # 依赖诊断独立保存，不进入发行包
        # 使用 pefile 分析 DLL 依赖，支持非 ASCII 源路径。
        "--experimental=force-dependencies-pefile",
        f"--include-data-dir={RESOURCES}=resources",  # 把 resources/ 打到包内根目录
        "--noinclude-data-files=resources/about/story/**",  # 项目短片在编译后外置，支持直接替换
        "--noinclude-data-files=resources/licenses/**",  # 许可按实际运行依赖收集并外置
        "--include-package=src",  # 包含延迟导入和条件导入的应用模块
        f"--output-dir={OUTPUT_DIR}",
        f"--output-filename={EXE_NAME}",
        "--assume-yes-for-downloads",  # 允许 Nuitka 自动拉取 MinGW64 / 依赖分析器
        # 元数据须为 ASCII：中文会被写进 C 头文件，GBK(936) 码页下 MSVC 报 C2001 编译失败
        f"--company-name={APP_ORGANIZATION}",
        f"--product-name={APP_NAME}",
        f"--file-version={APP_VERSION}",
        f"--product-version={APP_VERSION}",
        "--file-description=3DMigoto Mod Manager",
        # 在可执行文件清单中请求管理员权限，以支持目标目录写入及拖放注册。
        "--windows-uac-admin",
    ]
    if not args.console:
        cmd.append("--windows-console-mode=disable")

    cmd.extend(("--include-package-data=certifi", "--include-package=pypinyin", "--include-package=unrar"))

    if ICON.exists():
        cmd.append(f"--windows-icon-from-ico={ICON}")

    cmd += args.nuitka_args  # `--` 之后透传
    cmd.append(str(ENTRY))
    return cmd


def normalize_compilation_report(report: Path) -> None:
    """规范化 Nuitka 的 UTF-8 编码声明，使含中文路径的报告可由标准 XML 解析器读取。"""
    if not report.is_file():
        return
    contents = report.read_bytes()
    declaration, separator, body = contents.partition(b"\n")
    normalized = declaration.replace(b"encoding='utf8'", b"encoding='utf-8'")
    normalized = normalized.replace(b'encoding="utf8"', b'encoding="utf-8"')
    if normalized != declaration:
        report.write_bytes(normalized + separator + body)


def relocate_product(onefile: bool) -> Path:
    """将编译产物移入项目 build 目录。

    单文件模式移动可执行文件，目录模式移动 run_app.dist；源目标相同则直接返回。
    """
    if onefile:
        src = OUTPUT_DIR / EXE_NAME
        dest = PROJECT_BUILD / EXE_NAME
    else:
        src = OUTPUT_DIR / "run_app.dist"
        dest = PROJECT_BUILD / DIST_NAME
    if not src.exists():
        return src
    try:
        if src.resolve() == dest.resolve():
            return dest  # 暂存就在项目内，无需搬运
    except OSError:
        pass
    PROJECT_BUILD.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        if dest.is_dir():
            shutil.rmtree(dest, ignore_errors=True)
        else:
            try:
                dest.unlink()
            except OSError:
                pass
    shutil.move(str(src), str(dest))
    return dest


def _remove(path: Path) -> bool:
    """删除指定构建文件或目录，返回是否已完成删除。"""
    if not path.exists():
        return False
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
    else:
        try:
            path.unlink()
        except OSError:
            return False
    return not path.exists()


def clean_outputs() -> int:
    """清理本仓库的构建产物和编译暂存目录。"""
    documentation_outputs = (PROJECT_BUILD / "docs", PROJECT_BUILD / "tutorials")
    try:
        for directory in documentation_outputs:
            _validate_documentation_output(directory, PROJECT_BUILD)
    except ValueError as error:
        print(f"[X] {error}")
        return 1
    targets: list[Path] = [
        PROJECT_BUILD / DIST_NAME,
        PROJECT_BUILD / EXE_NAME,
        PROJECT_BUILD / "files.manifest",
        PROJECT_BUILD / "nuitka-report.xml",
        PROJECT_BUILD / "config" / "languages",
        PROJECT_BUILD / "plugins",
        PROJECT_BUILD / "resources" / "about" / "story",
        PROJECT_BUILD / UPDATER_SUBDIR,
        PROJECT_BUILD / "licenses",
        *documentation_outputs,
        *(PROJECT_BUILD / name for name in LEGAL_NOTICES),
        *sorted(PROJECT_BUILD.glob("*.zip")),
        STAGING_DIR,
    ]
    removed = 0
    for t in targets:
        if _remove(t):
            print(f"   [OK] 已删除 {t}")
            removed += 1
    # 暂存根目录为空时一并移除。
    if STAGING_ROOT != PROJECT_BUILD and STAGING_ROOT.is_dir():
        try:
            next(STAGING_ROOT.iterdir())
        except StopIteration:
            STAGING_ROOT.rmdir()
            print(f"   [OK] 已删除空暂存根目录 {STAGING_ROOT}")
        except OSError:
            pass
    if not removed:
        print("   [i] 没有可清理的产物（build/ 与暂存目录本来就是干净的）")
    return 0


def prepare_updater_exe() -> Path:
    """将已构建的原生 Release 更新器复制到输出目录。"""
    if not NATIVE_UPDATER_EXE.is_file():
        raise FileNotFoundError(
            f"未找到原生 x64 更新器：{NATIVE_UPDATER_EXE}\n"
            "请先执行 python updater/tools/build.py 生成 Release | x64 更新器。"
        )
    return embed_updater(PROJECT_BUILD, NATIVE_UPDATER_EXE)


def embed_updater(dist_dir: Path, updater_exe: Path) -> Path:
    """将原生更新器放入主程序目录下的更新器子目录。"""
    dest = dist_dir / UPDATER_SUBDIR / UPDATER_EXE_NAME
    dest.parent.mkdir(parents=True, exist_ok=True)
    if updater_exe.resolve() != dest.resolve():
        shutil.copy2(updater_exe, dest)
    return dest


def stage_language_packs(product_dir: Path) -> Path:
    """为目录模式和单文件模式复制默认语言配置。"""
    directory = product_dir / "config" / "languages"
    for relative in SHIPPED_LANGUAGE_FILES:
        target = directory / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(LANGUAGES / relative, target)
    return directory


def stage_project_story(product_dir: Path) -> Path:
    """复制清单指定的短片和封面，供两种打包模式读取。"""
    story = load_project_story(PROJECT_STORY)
    selected = {story.video_path, story.cover_path}
    for source in selected:
        if not source.is_file():
            raise FileNotFoundError(f"项目短片资源缺失：{source}")
    selected.update(PROJECT_STORY / name for name in ("story.json", "README.md") if (PROJECT_STORY / name).is_file())
    directory = product_dir / "resources" / "about" / "story"
    target, product, source_root = directory.resolve(), product_dir.resolve(), PROJECT_STORY.resolve()
    if (
        not target.is_relative_to(product)
        or target == product
        or target.is_relative_to(source_root)
        or source_root.is_relative_to(target)
    ):
        raise ValueError(f"项目短片输出目录无效：{directory}")
    if directory.exists():
        shutil.rmtree(directory)
    for source in sorted(selected):
        destination = directory / source.relative_to(PROJECT_STORY)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    return directory


def stage_plugins(product_dir: Path) -> Path:
    """复制随包教学插件的清单、源码、语言包与说明，排除本地安装文件。"""
    directory = product_dir / "plugins"
    if directory.exists():
        shutil.rmtree(directory)
    for plugin_id in SHIPPED_PLUGIN_IDS:
        for relative in SHIPPED_PLUGIN_FILES[plugin_id]:
            target = directory / plugin_id / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(PLUGINS / plugin_id / relative, target)
    return directory
