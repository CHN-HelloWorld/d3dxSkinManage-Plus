"""将 GameBanana 网络异常转换为界面提示。

优先按 httpx 异常类型分类，再按字符串特征处理其他异常。
未知错误使用通用说明，界面不显示原始堆栈和系统错误编号。
"""

from __future__ import annotations

import httpx

from src.utils.i18n import t as tr


def _status_message(code: int) -> str:
    """将常见 HTTP 错误状态映射为面向用户的本地化说明，其余状态使用通用失败提示。"""
    if code == 429:
        return tr("请求过于频繁，请稍后再试。")
    if code == 403:
        return tr("访问被拒绝（可能触发了地区或来源限制），请稍后重试。")
    if code == 404:
        return tr("内容不存在或已被删除。")
    if 500 <= code < 600:
        return tr("GameBanana 服务器繁忙，请稍后重试。")
    return tr("请求失败，请稍后重试。")


def _from_message(msg: str) -> str:
    """纯字符串特征回退处理：用于非 httpx 异常，或异常已被转成字符串的情形。"""
    m = (msg or "").lower()
    if "429" in m or "rate limit" in m or "too many" in m:
        return tr("请求过于频繁，请稍后再试。")
    if "deprecat" in m:
        return tr("GameBanana 接口已变更，建议更新本应用后重试。")
    if any(k in m for k in ("getaddrinfo", "name resolution", "nodename", "11001", "dns")):
        return tr("无法解析服务器地址，请检查网络连接后重试。")
    if "proxy" in m:
        return tr("代理连接失败，请检查代理设置或关闭代理后重试。")
    if any(k in m for k in ("timeout", "timed out")):
        return tr("连接超时，请检查网络后重试。")
    if any(k in m for k in ("ssl", "certificate", "certificate_verify")):
        return tr("安全连接失败（SSL），请检查系统时间或网络后重试。")
    if any(k in m for k in ("refused", "unreachable", "reset", "network", "connect")):
        return tr("网络连接失败，请检查网络后重试。")
    if "json" in m:
        return tr("服务器返回数据异常，请稍后重试。")
    # 未知错误使用通用提示，不回显堆栈或系统错误编号。
    return tr("加载失败，请稍后重试。")


def friendly_error(exc: BaseException) -> str:
    """将网络异常转换为简明的界面提示。"""
    # HTTP 状态码错误（按码分档）
    if isinstance(exc, httpx.HTTPStatusError):
        try:
            return _status_message(int(exc.response.status_code))
        except Exception:
            return tr("请求失败，请稍后重试。")
    # 代理错误（放在 NetworkError 前，因 ProxyError 是其子类）
    if isinstance(exc, httpx.ProxyError):
        return tr("代理连接失败，请检查代理设置或关闭代理后重试。")
    # 超时
    if isinstance(exc, httpx.TimeoutException):
        return tr("连接超时，请检查网络后重试。")
    # 根据消息特征进一步区分 DNS、SSL 等连接错误。
    if isinstance(exc, httpx.ConnectError):
        m = str(exc).lower()
        if any(k in m for k in ("getaddrinfo", "name resolution", "nodename", "11001", "dns")):
            return tr("无法解析服务器地址，请检查网络连接后重试。")
        if any(k in m for k in ("ssl", "certificate")):
            return tr("安全连接失败（SSL），请检查系统时间或网络后重试。")
        return tr("无法连接到 GameBanana，请检查网络后重试。")
    if isinstance(exc, httpx.NetworkError):
        return tr("网络连接失败，请检查网络后重试。")
    # 处理其余 HTTPError，包括响应解析失败。
    if isinstance(exc, httpx.HTTPError):
        if "json" in str(exc).lower():
            return tr("服务器返回数据异常，请稍后重试。")
        return tr("网络请求失败，请检查网络后重试。")
    # 非 httpx 异常：字符串回退处理
    return _from_message(str(exc))
