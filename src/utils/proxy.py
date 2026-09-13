"""统一解析系统或手动代理配置。

新请求读取当前生效的代理 URL，已创建的 HTTP 客户端须在设置变化后显式更新连接配置。
"""

from __future__ import annotations

import sys
from typing import Any

import httpx

from src.utils.i18n import t as tr
from src.utils.logger import logger


def _read_windows_system_proxy() -> str | None:
    """从 Windows 注册表读取系统代理设置，返回形如 "http://127.0.0.1:7890" 的 URL。"""
    if sys.platform != "win32":
        return None
    try:
        import winreg

        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
        )
        try:
            proxy_enable, _ = winreg.QueryValueEx(key, "ProxyEnable")
            if not proxy_enable:
                return None
            proxy_server, _ = winreg.QueryValueEx(key, "ProxyServer")
            if not proxy_server:
                return None
            # 多协议设置优先使用 HTTPS 对应的代理。
            if "=" in proxy_server:
                parts = dict(p.split("=", 1) for p in proxy_server.split(";") if "=" in p)
                proxy_server = parts.get("https") or parts.get("http") or next(iter(parts.values()), "")
            if not proxy_server:
                return None
            if not proxy_server.startswith(("http://", "https://", "socks")):
                proxy_server = f"http://{proxy_server}"
            return proxy_server
        finally:
            winreg.CloseKey(key)
    except FileNotFoundError:
        return None
    except Exception as exc:
        logger.warning("读取系统代理失败: %s", exc)
        return None


def get_system_proxy() -> str | None:
    """对外暴露的系统代理读取（Windows 注册表，跨平台时返回 None）。"""
    return _read_windows_system_proxy()


def _master_active(config: Any) -> bool:
    """代理总开关（常规页）是否开启。"""
    return bool(config.get("gamebanana.proxy_config.active"))


def _resolve_proxy(config: Any) -> str | None:
    """在「总开关已开启」前提下解析实际代理 URL（system / manual）。

    代理设置统一来自 gamebanana.proxy_config，字段默认值和有效性由配置层处理。
    """
    mode = config.get("gamebanana.proxy_config.mode")
    if mode == "system":
        return get_system_proxy()
    if mode == "manual":
        ptype = (config.get("gamebanana.proxy_config.type") or "http").strip()
        host = (config.get("gamebanana.proxy_config.host") or "").strip()
        port = str(config.get("gamebanana.proxy_config.port") or "").strip()
        if not host or not port:
            return None
        return f"{ptype}://{host}:{port}"
    return None


def get_configured_proxy_url(config: Any = None) -> str | None:
    """返回「代理总开关」开启时所配置的代理 URL，总开关关闭则 None。

    用于**软件更新**：代理配置无条件生效（仅受总开关约束），与 Mod 商店「启用代理」无关。
    """
    if config is None:
        from src.core.config import Config

        config = Config()
    if not _master_active(config):
        return None
    return _resolve_proxy(config)


def get_active_proxy_url(config: Any = None) -> str | None:
    """返回 **Mod 商店** 当前生效的代理 URL，未启用返回 None。

    需同时满足：代理总开关(active) 开启 + Mod 商店「启用代理」(enabled) 开启。
    """
    if config is None:
        from src.core.config import Config

        config = Config()
    if not _master_active(config):
        return None
    if not config.get("gamebanana.proxy_config.enabled"):
        return None
    return _resolve_proxy(config)


def test_proxy_reachable(proxy: str | None, timeout: float = 5.0) -> tuple[bool, str]:
    """测试代理服务器端口的 TCP 可达性，返回状态与说明。

    只验证代理端口握手，不请求外部站点；端口可达不代表代理认证或目标站点访问成功。
    """
    import socket
    import time
    from urllib.parse import urlparse

    if not proxy:
        return False, tr("未配置代理")
    parsed = urlparse(proxy if "://" in proxy else f"http://{proxy}")
    host = parsed.hostname
    port = parsed.port
    if not host or not port:
        return False, tr("代理地址无法解析出主机和端口")
    try:
        start = time.perf_counter()
        with socket.create_connection((host, port), timeout=timeout):
            latency_ms = int((time.perf_counter() - start) * 1000)
        return True, tr("代理可连通 ({host}:{port}，延迟 {latency_ms} ms)", host=host, port=port, latency_ms=latency_ms)
    except TimeoutError:
        return False, tr("连接代理超时 ({host}:{port})", host=host, port=port)
    except ConnectionRefusedError:
        return False, tr("代理拒绝连接 ({host}:{port})，请确认代理已启动", host=host, port=port)
    except OSError as exc:
        return False, tr("无法连接代理 ({host}:{port}): {exc}", host=host, port=port, exc=exc)
    except Exception as exc:
        return False, tr("测试失败: {exc}", exc=exc)


# 使用不返回正文的连通性检测端点，204 表示正常响应，重定向用于识别门户拦截。
# 首个端点失败后尝试备用端点。
_LATENCY_TEST_URLS = (
    "https://cp.cloudflare.com/generate_204",
    "https://www.gstatic.com/generate_204",
)


def measure_proxy_latency(proxy: str | None, timeout: float = 6.0) -> tuple[bool, str]:
    """测量经代理访问外网的往返延迟，返回连接状态与提示。

    先检查代理端口，再发送不跟随重定向的 HEAD 请求。
    端口握手仅用于快速判断可达性；HTTP 往返才计入延迟，重定向用于识别门户拦截。
    """
    import time

    if not proxy:
        return False, tr("未配置代理")

    # 先检查代理端口，便于区分本地连接失败与外部请求失败。
    reachable, reach_msg = test_proxy_reachable(proxy, timeout=min(timeout, 3.0))
    if not reachable:
        return False, reach_msg

    # 经代理请求外部端点，测量 HTTP 往返延迟。
    last_err = ""
    try:
        with httpx.Client(proxy=proxy, timeout=timeout, follow_redirects=False) as client:
            for url in _LATENCY_TEST_URLS:
                try:
                    start = time.perf_counter()
                    resp = client.head(url)  # HEAD 请求不读取响应正文，减少探测流量。
                    latency_ms = int((time.perf_counter() - start) * 1000)
                    if resp.status_code in (200, 204):
                        return True, tr("连接正常，延迟 {latency_ms} ms（经代理访问外网）", latency_ms=latency_ms)
                    if 300 <= resp.status_code < 400:
                        return False, (
                            tr(
                                "疑似被网关/门户劫持（HTTP {status_code}），代理可能未真正联网",
                                status_code=resp.status_code,
                            )
                        )
                    # 其它状态码：仍算连通（能收到响应），但如实报出状态码
                    return True, tr(
                        "连接正常，延迟 {latency_ms} ms（HTTP {status_code}）",
                        latency_ms=latency_ms,
                        status_code=resp.status_code,
                    )
                except httpx.HTTPError as exc:
                    last_err = str(exc)
                    continue  # 换下一个测试端点重试
    except Exception as exc:
        return False, tr("测试失败: {exc}", exc=exc)
    return False, tr("代理端口可达但无法访问外网：{value}", value=last_err or tr("目标端点均不可达"))
