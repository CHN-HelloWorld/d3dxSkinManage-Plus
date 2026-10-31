"""开发工具、安装器与运行时共用的插件发布前检查。"""

from src.plugin_sdk import API_VERSION
from src.plugins.runtime.manifest import Manifest, ManifestError, parse_manifest
from src.plugins.runtime.security import scan_plugin
from src.services.app_update.checker import compare_versions
from src.utils.constants import APP_VERSION
from src.utils.i18n import t as tr


def ensure_compatible(manifest: Manifest) -> None:
    """要求插件 API 主版本相同，且所需 API 和应用版本不高于宿主。"""
    if manifest.major_api != int(API_VERSION.split(".")[0]) or compare_versions(API_VERSION, manifest.api_version) < 0:
        raise ManifestError(
            tr(
                "API 版本不兼容: 插件需 {required}，宿主提供 {available}",
                required=manifest.api_version,
                available=API_VERSION,
            )
        )
    if compare_versions(APP_VERSION, manifest.min_app_version) < 0:
        raise ManifestError(
            tr(
                "应用版本不兼容: 插件需 {required}，宿主提供 {available}",
                required=manifest.min_app_version,
                available=APP_VERSION,
            )
        )


def validate_package(directory) -> Manifest:
    """验证清单、宿主版本和全部 Python 源码，不执行插件代码。"""
    manifest = parse_manifest(directory)
    ensure_compatible(manifest)
    scan = scan_plugin(directory, manifest.permissions)
    if not scan.ok:
        raise ManifestError(tr("安全扫描未通过: {details}", details=scan.summary()))
    return manifest
