"""常量定义"""

from src.utils.language_pack import N_

# 应用信息
APP_NAME = "d3dxSkinManage-Plus"
APP_VERSION = "0.1.0"
APP_DISPLAY_NAME = "d3dxSkinManage-Plus"
APP_ORGANIZATION = "d3dxSkinManage-Plus"

# 程序自身自动更新
# 打包后主程序的可执行文件名（standalone 模式下 build.py 的 EXE_NAME，须与之一致）。
MAIN_EXE_NAME = "d3dxSkinManage-Plus.exe"
# 独立更新器可执行文件名 + 其在安装目录内的子目录（updater 项目构建产物落点）。
UPDATER_EXE_NAME = "updater.exe"
UPDATER_SUBDIR = "Updater"
# GitHub 仓库（owner/repo）。留空表示「未配置更新源」——可由 config `app_update.repo` 覆盖。
# 例：填 "your-name/d3dxSkinManage-Plus" 后即可从该仓库的 Releases 拉取更新。
APP_UPDATE_REPO = ""
# 启动令牌密钥：主程序生成、updater.exe 校验，确保更新器只能被主程序合法拉起。
# 注意：原生更新器 Updater/src/token.cpp 内必须使用完全相同的字面量。
APP_UPDATE_TOKEN_SECRET = b"d3dxSkinManagePlus_Updater_LaunchToken_2026!"
# 更新检查 GitHub API 镜像竞速前缀（国内直连 api.github.com 常超慢/不可达）。
# 空串=官方直连；其余为加速镜像。谁先返回合法 JSON 用谁。
APP_UPDATE_API_MIRRORS = (
    "",
    "https://gh-proxy.com/",
    "https://ghfast.top/",
    "https://gh.llkk.cc/",
)
# 更新包下载源。
# 每项 (key, 显示名, URL前缀)；前缀为空=官方直连。UI 另提供「auto」=镜像优先、失败回退。
# 镜像为「前缀型」release 代理：把原始 GitHub 资产 URL 拼到前缀后即可加速。
APP_UPDATE_DOWNLOAD_MIRRORS = (
    ("official", N_("GitHub 官方直连"), ""),
    ("ghproxy", N_("gh-proxy 加速"), "https://gh-proxy.com/"),
    ("ghfast", N_("ghfast 加速"), "https://ghfast.top/"),
    ("ghproxynet", N_("ghproxy.net 加速"), "https://ghproxy.net/"),
    ("llkk", N_("gh.llkk.cc 加速"), "https://gh.llkk.cc/"),
)

# 文件扩展名
ARCHIVE_EXTENSIONS = {".zip", ".7z", ".rar"}
# 可直接作为 Mod 本体存储的归档格式。
RESIDENT_ARCHIVE_EXTENSIONS = {".zip", ".7z", ".rar"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp"}

# Mod 分享/本地元数据信封目录名与清单名（跨 archive/mod_package 共用的底层字面量，
# 放这里避免 archive→core 的反向依赖）。点开头目录，库扫描与多数解压工具自然忽略。
META_DIR = ".d3dxmeta"
MANIFEST_NAME = "info.json"


def is_resident_archive(path) -> bool:
    """该路径是否为库内常驻压缩档（zip/7z/rar 单文件 Mod）。"""
    from pathlib import Path as _P

    return _P(path).suffix.lower() in RESIDENT_ARCHIVE_EXTENSIONS


# 3DMigoto 标识；部署目录由用户显式指定。
MIGOTO_LOADER_NAME = "3DMigoto Loader.exe"
MIGOTO_DISABLED_PREFIX = "DISABLED"

# Mod 管理
CATEGORY_MARKER_FILE = ".d3dxcategory"  # 类别标识文件
# Mod 容器标记。容器包含本体及同级 .d3dxmeta，元数据可独立编辑，无需重新压缩本体。
# 发现器依靠标记识别边界，复制完整容器可保留其元数据。
MOD_CONTAINER_MARKER = ".d3dxmod"

# UI 相关
MIN_WINDOW_WIDTH = 1024
MIN_WINDOW_HEIGHT = 680
DEFAULT_WINDOW_WIDTH = 1400
DEFAULT_WINDOW_HEIGHT = 900
