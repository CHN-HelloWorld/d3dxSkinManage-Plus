from src.services.gamebanana.models import GbModel
from src.services.gamebanana.url_helper import (
    build_mod_page_url,
    parse_gb_url,
)


class TestParseGbUrl:
    def test_parse_mod_url(self):
        assert parse_gb_url("https://gamebanana.com/mods/123456") == (GbModel.MOD, 123456)

    def test_parse_wip_url(self):
        assert parse_gb_url("https://gamebanana.com/wips/789") == (GbModel.WIP, 789)

    def test_parse_tool_url(self):
        assert parse_gb_url("https://gamebanana.com/tools/42") == (GbModel.TOOL, 42)

    def test_parse_with_query_string(self):
        assert parse_gb_url("https://gamebanana.com/mods/123?tab=files") == (GbModel.MOD, 123)

    def test_parse_with_fragment(self):
        assert parse_gb_url("https://gamebanana.com/mods/123#header") == (GbModel.MOD, 123)

    def test_parse_http_scheme(self):
        assert parse_gb_url("http://gamebanana.com/mods/123") == (GbModel.MOD, 123)

    def test_parse_invalid_returns_none(self):
        assert parse_gb_url("https://example.com/whatever") is None
        assert parse_gb_url("not a url") is None
        assert parse_gb_url("") is None
        assert parse_gb_url("https://gamebanana.com/mods/notanumber") is None
        assert parse_gb_url("https://gamebanana.com/unknown/123") is None


class TestBuildModPageUrl:
    def test_build_mod(self):
        assert build_mod_page_url(GbModel.MOD, 123) == "https://gamebanana.com/mods/123"

    def test_build_wip(self):
        assert build_mod_page_url(GbModel.WIP, 7) == "https://gamebanana.com/wips/7"

    def test_build_tool(self):
        assert build_mod_page_url(GbModel.TOOL, 99) == "https://gamebanana.com/tools/99"
