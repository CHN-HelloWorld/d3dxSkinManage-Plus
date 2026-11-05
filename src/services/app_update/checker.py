"""查询 GitHub Releases、选择镜像、比较版本并解析更新资源。

使用 httpx 同步客户端并透传代理设置。镜像请求在线程池内并发执行，
采用最先返回的有效 JSON。模块不依赖 Qt，可由工作线程调用。
"""

from __future__ import annotations

import re
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from functools import cmp_to_key
from typing import Any

import httpx

from src.utils.constants import APP_UPDATE_API_MIRRORS, APP_UPDATE_REPO, APP_VERSION
from src.utils.i18n import t as tr
from src.utils.logger import logger


@dataclass
class AppUpdateInfo:
    """一次更新检查的结构化结果。"""

    has_update: bool = False
    current_version: str = APP_VERSION
    latest_version: str = ""
    tag_name: str = ""
    release_name: str = ""
    changelog: str = ""
    download_url: str = ""
    download_size: int = 0
    sha256: str = ""
    error: str | None = None
    raw: dict = field(default_factory=dict)
    releases: list[dict[str, str]] = field(default_factory=list)
    history_error: str = ""
    repo: str = ""
    asset_id: int = 0
    asset_name: str = ""

    def to_dict(self) -> dict:
        """导出供界面使用的检查结果并复制版本说明列表，不包含原始网络响应。"""
        return {
            "has_update": self.has_update,
            "current_version": self.current_version,
            "latest_version": self.latest_version,
            "tag_name": self.tag_name,
            "release_name": self.release_name,
            "changelog": self.changelog,
            "download_url": self.download_url,
            "download_size": self.download_size,
            "sha256": self.sha256,
            "error": self.error,
            "releases": [dict(release) for release in self.releases],
            "history_error": self.history_error,
            "repo": self.repo,
            "asset_id": self.asset_id,
            "asset_name": self.asset_name,
        }


def compare_versions(version1: str, version2: str) -> int:
    """语义化版本比较：>0 表示 v1 更新，<0 表示 v2 更新，=0 相同。

    容忍 "v" 前缀与 "-beta" 等后缀（只取每段开头的数字）。
    """

    def parse(v: str) -> list[int]:
        """去除版本前缀并提取各点分段的前导数字，无法解析的分段按零参与比较。"""
        v = (v or "").lstrip("vV")
        out: list[int] = []
        for part in v.split("."):
            m = re.match(r"(\d+)", part)
            out.append(int(m.group(1)) if m else 0)
        return out

    a, b = parse(version1), parse(version2)
    n = max(len(a), len(b))
    a += [0] * (n - len(a))
    b += [0] * (n - len(b))
    for x, y in zip(a, b):
        if x != y:
            return 1 if x > y else -1
    return 0


class AppUpdateChecker:
    """程序更新检查器。"""

    def __init__(
        self,
        current_version: str = APP_VERSION,
        repo: str | None = None,
        proxy: str | None = None,
    ) -> None:
        """保存当前版本、仓库及代理配置，清理仓库路径边界但不立即发起网络请求。"""
        self.current_version = current_version
        self.repo = (repo or APP_UPDATE_REPO or "").strip().strip("/")
        self.proxy = proxy or None

    def check(self, timeout: int = 15) -> AppUpdateInfo:
        """执行一次检查，返回 AppUpdateInfo（错误经 .error 返回，不抛出）。"""
        info = AppUpdateInfo(current_version=self.current_version, repo=self.repo)

        if not self.repo or "/" not in self.repo:
            info.error = tr("更新来源配置无效，无法检查更新。")
            logger.info("程序更新检查跳过：%s", info.error)
            return info

        api_path = f"https://api.github.com/repos/{self.repo}/releases/latest"
        try:
            release = self._race_fetch(api_path, timeout, self.proxy)
        except Exception as e:
            # 代理请求失败后直连重试一次，仍失败时返回错误。
            if self.proxy:
                logger.warning("程序更新：代理检查失败(%s)，降级直连重试", e)
                try:
                    release = self._race_fetch(api_path, timeout, None)
                except Exception as e2:
                    info.error = tr("无法获取更新信息，请稍后重试。")
                    logger.warning("程序更新检查失败（直连亦失败）: %s", e2)
                    return info
            else:
                info.error = tr("无法获取更新信息，请稍后重试。")
                logger.warning("程序更新检查失败: %s", e)
                return info

        info.raw = release
        info.tag_name = release.get("tag_name", "") or ""
        info.latest_version = info.tag_name.lstrip("vV")
        info.release_name = release.get("name", "") or ""
        info.changelog = release.get("body", "") or ""
        self._load_history(info, release, timeout)

        info.has_update = compare_versions(info.latest_version, self.current_version) > 0
        if not info.has_update:
            logger.info("已是最新版本（当前 %s，最新 %s）", self.current_version, info.latest_version)
            return info

        # 附件身份只取自所选 ZIP；其它附件及历史发布不能提供当前下载的摘要。
        asset_digest = ""
        for asset in release.get("assets", []) or []:
            name = (asset.get("name", "") or "").lower()
            if name.endswith(".zip"):
                info.download_url = asset.get("browser_download_url", "") or ""
                size, asset_id = asset.get("size"), asset.get("id")
                info.download_size = size if type(size) is int and size > 0 else 0
                info.asset_id = asset_id if type(asset_id) is int and asset_id > 0 else 0
                info.asset_name = asset.get("name", "") or ""
                digest = asset.get("digest")
                match = re.fullmatch(r"sha256:([a-fA-F0-9]{64})", digest) if isinstance(digest, str) else None
                if match:
                    asset_digest = match.group(1).lower()
                break

        # 兼容已有发布正文的摘要；两处有效摘要冲突时拒绝自动更新。
        match = re.search(r"SHA256[:\s]+([a-fA-F0-9]{64})(?![a-fA-F0-9])", info.changelog)
        body_digest = match.group(1).lower() if match else ""
        if asset_digest and body_digest and asset_digest != body_digest:
            info.error = tr("更新附件与发布说明中的 SHA-256 不一致，请稍后重新检查更新。")
            return info
        info.sha256 = asset_digest or body_digest

        logger.info("发现新版本 %s（资产 %.1fMB）", info.latest_version, info.download_size / 1024 / 1024)
        return info

    def _load_history(self, info: AppUpdateInfo, latest: dict, timeout: int) -> None:
        """历史版本仅用于展示，安装目标始终取自 /latest。"""
        info.releases = [self._release_notes(latest)]
        api_path = f"https://api.github.com/repos/{self.repo}/releases?per_page=30"
        try:
            try:
                releases = self._race_fetch(api_path, min(timeout, 8), self.proxy)
            except Exception:
                if not self.proxy:
                    raise
                releases = self._race_fetch(api_path, min(timeout, 8), None)
            seen = {info.latest_version.casefold()}
            history = []
            for release in releases:
                if not isinstance(release, dict) or release.get("draft") or release.get("prerelease"):
                    continue
                tag = release.get("tag_name")
                if not isinstance(tag, str) or not tag.strip():
                    continue
                notes = self._release_notes(release)
                key = notes["version"].casefold()
                if key not in seen:
                    seen.add(key)
                    history.append(notes)
            history.sort(key=cmp_to_key(lambda a, b: compare_versions(b["version"], a["version"])))
            info.releases.extend(history[:29])
        except Exception as exc:  # 历史版本查询失败不应阻止有效更新。
            info.history_error = tr("暂时无法获取历史版本，当前显示最新版本的更新说明。")
            logger.info("程序更新：历史版本获取失败，保留最新发布信息: %s", exc)

    @staticmethod
    def _release_notes(release: dict) -> dict[str, str]:
        """从发布记录提取界面所需的版本说明字段，非字符串字段归为空串。"""

        def text(key: str) -> str:
            """读取发布记录的字符串字段，缺失或类型不符时返回空串。"""
            value = release.get(key)
            return value if isinstance(value, str) else ""

        return {
            "version": text("tag_name").strip().lstrip("vV"),
            "tag_name": text("tag_name"),
            "release_name": text("name"),
            "changelog": text("body"),
            "published_at": text("published_at"),
            "html_url": text("html_url"),
        }

    def _race_fetch(self, api_path: str, timeout: int, proxy: str | None) -> dict | list[Any]:
        """官方直连 + 各镜像并发竞速，第一个返回合法 JSON 的胜出。proxy 为本次使用的代理。"""
        urls = [(p + api_path) if p else api_path for p in APP_UPDATE_API_MIRRORS]
        headers = {
            "User-Agent": "d3dxSkinManage-Plus-Updater",
            "Accept": "application/vnd.github.v3+json",
        }

        wants_history = api_path.split("?", 1)[0].endswith("/releases")

        def fetch(url: str) -> dict | list[Any]:
            """在线程内使用独立 HTTP 客户端获取候选源并校验响应形状，退出时关闭连接，错误向上传递。"""
            with httpx.Client(proxy=proxy, follow_redirects=True, timeout=timeout) as client:
                resp = client.get(url, headers=headers)
                resp.raise_for_status()
                data = resp.json()
                if wants_history:
                    if not isinstance(data, list):
                        raise ValueError("Response is not a release list")
                elif not isinstance(data, dict) or not isinstance(data.get("tag_name"), str) or not data["tag_name"]:
                    raise ValueError(tr("响应不是合法的 Release JSON"))
                return data

        last_err: Exception | None = None
        pool = ThreadPoolExecutor(max_workers=len(urls))
        try:
            futures = {pool.submit(fetch, u): u for u in urls}
            pending = set(futures)
            while pending:
                done, pending = wait(pending, timeout=timeout + 2, return_when=FIRST_COMPLETED)
                if not done:
                    break
                for fut in done:
                    try:
                        result = fut.result()
                        return result
                    except Exception as e:
                        last_err = e
        finally:
            # 不等待慢源；已运行的请求在完成或超时后自行关闭客户端。
            pool.shutdown(wait=False, cancel_futures=True)
        raise last_err or RuntimeError(tr("所有更新源均无响应"))
