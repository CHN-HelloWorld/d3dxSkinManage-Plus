"""为更新下载测试提供固定附件和可中断的 HTTP 响应体。"""

from __future__ import annotations

import hashlib

import httpx


class InterruptedBody(httpx.AsyncByteStream):
    """先交付指定前缀，再模拟连接中断，检验已落盘内容的恢复。"""

    def __init__(self, prefix: bytes) -> None:
        """保存本轮允许交付的字节。"""
        self.prefix = prefix

    async def __aiter__(self):
        """交付前缀后抛出真实 HTTP 传输异常。"""
        yield self.prefix
        raise httpx.ReadError("synthetic interrupted body")


def make_session(tmp_path, payload, urls, **kwargs):
    """构造固定版本的真实会话，仅用测试策略关闭额外测速和自动重试。"""
    from src.services.app_update.download import DownloadSession
    from src.services.app_update.download_sources import DownloadPolicy, DownloadRoute
    from src.services.app_update.download_state import UpdateArtifact

    artifact = UpdateArtifact(
        url="https://origin.test/v2/app.zip",
        size=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
        repo="example/app",
        tag="v2",
        asset_id=42,
        name="app.zip",
    )
    return DownloadSession(
        artifact,
        tmp_path,
        [DownloadRoute(f"source-{index}", url) for index, url in enumerate(urls)],
        automatic=kwargs.pop("automatic", False),
        policy=kwargs.pop("policy", DownloadPolicy(network_retries=0)),
        **kwargs,
    )
