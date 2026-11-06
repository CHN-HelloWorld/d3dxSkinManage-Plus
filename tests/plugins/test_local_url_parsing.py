"""区分本地 URL 字符串解析与实际网络客户端的插件权限。"""

import pytest

from src.plugins.runtime.security import scan_plugin


@pytest.mark.parametrize(
    "statement", ["from urllib.parse import urlsplit", "import urllib.parse", "from urllib import parse"]
)
def test_url_parsing_does_not_require_network(tmp_path, statement):
    (tmp_path / "entry.py").write_text(statement, encoding="utf-8")
    result = scan_plugin(tmp_path, ())
    assert result.ok, result.summary()


@pytest.mark.parametrize(
    "statement",
    ["import urllib.request", "import urllib", "from urllib import parse, request", "import urllib.parse_extra"],
)
def test_network_modules_still_require_network(tmp_path, statement):
    (tmp_path / "entry.py").write_text(statement, encoding="utf-8")
    forbidden = scan_plugin(tmp_path, ())
    assert not forbidden.ok and forbidden.ungranted
    assert scan_plugin(tmp_path, {"network"}).ok
