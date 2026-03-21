"""验证 errors.friendly_error 将 GameBanana 异常转换为中文提示并隐藏堆栈和 errno。"""

from src.services.gamebanana.errors import friendly_error


def test_friendly_429():
    assert "频繁" in friendly_error(Exception("HTTP 429 Too Many Requests"))


def test_friendly_deprecation():
    assert "变更" in friendly_error(Exception("Deprecation header detected"))


def test_friendly_network():
    for msg in (
        "Network unreachable",
        "Connection refused",
        "timeout while reading",
        "Temporary failure in name resolution",
    ):
        assert "网络" in friendly_error(Exception(msg)), msg


def test_friendly_unknown_hides_stack():
    # 未知异常绝不回显原始堆栈 / errno
    text = friendly_error(Exception("something exotic [Errno 12345] getaddrinfo"))
    # getaddrinfo 命中 DNS 分支属可接受；关键是不泄漏 exotic / Errno 数字
    assert "exotic" not in text
    assert "12345" not in text
