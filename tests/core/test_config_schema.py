from __future__ import annotations

import json

import pytest


def _settings_path(temp_data_dir):
    return temp_data_dir / "config" / "settings.json"


def _write(temp_data_dir, payload) -> None:
    p = _settings_path(temp_data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _invalid_backups(temp_data_dir) -> list:
    return sorted((temp_data_dir / "config").glob("settings.json.invalid-*"))


def _raw(temp_data_dir) -> dict:
    return json.loads(_settings_path(temp_data_dir).read_text(encoding="utf-8"))


class TestSchemaAuthority:
    """_DEFAULT_SETTINGS 是第一方配置键的唯一权威定义。"""

    def test_missing_file_writes_defaults_with_schema_version(self, temp_data_dir):
        from src.core.config import SETTINGS_SCHEMA_VERSION, Config

        cfg = Config()
        assert _settings_path(temp_data_dir).exists()
        assert cfg.get("schema_version") == SETTINGS_SCHEMA_VERSION
        assert _raw(temp_data_dir)["schema_version"] == SETTINGS_SCHEMA_VERSION

    def test_legacy_proxy_string_key_removed_from_schema(self, temp_data_dir):
        from src.core.config import Config

        assert "proxy" not in Config._DEFAULT_SETTINGS["gamebanana"]

    @pytest.mark.parametrize(
        "dotted",
        [
            "gamebanana.proxy",  # 代理改走 proxy_config
            "import.default_view",  # 视图模式改为按页记忆 ui.*_view_mode
            "gamebanana.default_model_filter",  # 「全部/仅 Mods」下拉已移除
            "gamebanana.download_cache_max_gb",  # 下载产物已迁 data/downloads=用户资产
            "gamebanana.hover_scale_enabled",  # 悬停缩放恒开、从未对外开放
            "gamebanana.update_check.ignored_mods",  # 忽略状态改存 mod.custom_data.gamebanana
            "import.deploy_method",  # 库内只存压缩档 → 部署恒为独立副本，无可选项
        ],
    )
    def test_removed_keys_are_out_of_schema(self, temp_data_dir, dotted):
        """未被声明的配置键不参与程序行为，保留其文件内容。"""
        from src.core.config import Config

        node = Config._DEFAULT_SETTINGS
        parts = dotted.split(".")
        for k in parts[:-1]:
            node = node.get(k, {}) if isinstance(node, dict) else {}
        assert parts[-1] not in node, f"{dotted} 应已从默认 schema 删除"


class TestNoWriteBack:
    """默认值仅在读取时使用，不自动回写用户文件。"""

    def test_defaults_are_not_written_back_into_user_file(self, temp_data_dir):
        from src.core.config import Config

        _write(temp_data_dir, {"general": {"theme": "light"}})
        cfg = Config()

        assert cfg.get("general.theme") == "light"  # 用户值
        assert cfg.get("import.format") == "7z"  # 内置默认回退处理

        on_disk = _raw(temp_data_dir)
        assert "import" not in on_disk, "默认值不得递归补进用户文件"
        assert on_disk["general"] == {"theme": "light"}

    def test_set_persists_only_touched_keys(self, temp_data_dir):
        from src.core.config import Config

        _write(temp_data_dir, {"general": {"theme": "light"}})
        cfg = Config()
        cfg.set("ui.window_width", 1280)

        on_disk = _raw(temp_data_dir)
        assert on_disk["ui"] == {"window_width": 1280}
        assert on_disk["general"] == {"theme": "light"}
        assert "gamebanana" not in on_disk

    def test_get_returns_copy_of_container_default(self, temp_data_dir):
        from src.core.config import Config

        cfg = Config()
        bg = cfg.get("appearance.background")
        bg["path"] = "D:/hacked.png"
        assert cfg.get("appearance.background.path") == ""


class TestPartialSettings:
    """缺省配置项使用默认值，明确设置和自定义键按原文件保留。"""

    def test_missing_optional_key_keeps_user_values(self, temp_data_dir):
        from src.core.config import Config

        _write(
            temp_data_dir,
            {
                "schema_version": 1,
                "general": {"theme": "light", "language": "en-US"},
                "ui": {"window_width": 1600},
                # 缺失的 import.strict_format 应使用内置默认值。
                "import": {"format": "7z"},
            },
        )
        cfg = Config()

        assert cfg.get("general.theme") == "light"
        assert cfg.get("general.language") == "en-US"
        assert cfg.get("ui.window_width") == 1600
        assert cfg.get("import.format") == "7z"
        assert cfg.get("import.strict_format") is False
        assert not _invalid_backups(temp_data_dir), "缺字段不是损坏，不得备份重建"

    def test_missing_schema_version_is_adopted_not_rebuilt(self, temp_data_dir):
        from src.core.config import SETTINGS_SCHEMA_VERSION, Config

        _write(temp_data_dir, {"general": {"theme": "light"}})
        cfg = Config()

        assert cfg.get("general.theme") == "light"
        assert not _invalid_backups(temp_data_dir)
        cfg.set("ui.window_height", 900)
        assert _raw(temp_data_dir)["schema_version"] == SETTINGS_SCHEMA_VERSION

    def test_unknown_keys_are_preserved(self, temp_data_dir):
        from src.core.config import Config

        _write(
            temp_data_dir,
            {
                "schema_version": 1,
                "general": {"theme": "light"},
                "my_experiment": {"a": 1},
            },
        )
        cfg = Config()
        assert cfg.get("general.theme") == "light"
        assert not _invalid_backups(temp_data_dir)

        cfg.set("ui.window_width", 1024)
        on_disk = _raw(temp_data_dir)
        assert on_disk["my_experiment"] == {"a": 1}


class TestPerKeyFallback:
    """单项配置无效时使用该项默认值，其他配置保持原样。"""

    def test_invalid_enum_value_falls_back_only_that_key(self, temp_data_dir):
        from src.core.config import Config

        _write(
            temp_data_dir,
            {
                "schema_version": 1,
                "general": {"theme": "light"},
                "import": {"format": "tar.gz", "parallel_workers": 5},
            },
        )
        cfg = Config()

        assert cfg.get("import.format") == "7z"  # 只有这一项回默认
        assert cfg.get("import.parallel_workers") == 5
        assert cfg.get("general.theme") == "light"
        assert not _invalid_backups(temp_data_dir)

    def test_invalid_type_falls_back_only_that_key(self, temp_data_dir):
        from src.core.config import Config

        _write(
            temp_data_dir,
            {
                "schema_version": 1,
                "ui": {"window_width": "很宽", "window_height": 900},
            },
        )
        cfg = Config()

        assert cfg.get("ui.window_width") == 0
        assert cfg.get("ui.window_height") == 900
        assert not _invalid_backups(temp_data_dir)

    def test_broken_section_falls_back_only_that_section(self, temp_data_dir):
        from src.core.config import Config

        _write(
            temp_data_dir,
            {
                "schema_version": 1,
                "general": {"theme": "light"},
                "import": "这里本该是个对象",
            },
        )
        cfg = Config()

        assert cfg.get("import.format") == "7z"
        assert cfg.get("import.parallel_workers") == 0
        assert cfg.get("general.theme") == "light"
        assert not _invalid_backups(temp_data_dir)

    def test_bool_field_rejects_int(self, temp_data_dir):
        from src.core.config import Config

        _write(temp_data_dir, {"schema_version": 1, "import": {"strict_format": 1}})
        assert Config().get("import.strict_format") is False

    def test_int_field_rejects_bool(self, temp_data_dir):
        from src.core.config import Config

        _write(temp_data_dir, {"schema_version": 1, "import": {"parallel_workers": True}})
        assert Config().get("import.parallel_workers") == 0

    def test_epoch_float_field_is_preserved(self, temp_data_dir):
        """真实配置里的 last_check_time 是 time.time 的浮点值，不能被当成坏值丢掉
        —— 丢了等于把程序更新的 24h 节流清零，每次启动都重新联网检查。"""
        from src.core.config import Config

        _write(
            temp_data_dir,
            {
                "schema_version": 1,
                "app_update": {"last_check_time": 1787577232.5254161},
            },
        )
        assert Config().get("app_update.last_check_time") == 1787577232.5254161

    def test_free_form_map_contents_are_not_validated(self, temp_data_dir):
        from src.core.config import Config

        _write(
            temp_data_dir,
            {
                "schema_version": 1,
                "gamebanana": {"proxy_config": {"active": True, "enabled": True}},
            },
        )
        cfg = Config()
        assert cfg.get("gamebanana.proxy_config.active") is True
        assert cfg.get("gamebanana.proxy_config.enabled") is True

    def test_nullable_field_accepts_none_and_list(self, temp_data_dir):
        from src.core.config import Config

        _write(
            temp_data_dir,
            {
                "schema_version": 1,
                "ui": {"game_list_col_widths": [10, 20, 30]},
            },
        )
        assert Config().get("ui.game_list_col_widths") == [10, 20, 30]


class TestWholeFileRebuild:
    """只有真·不可解读才备份重建。"""

    def test_broken_json_backs_up_and_rebuilds(self, temp_data_dir):
        from src.core.config import Config

        p = _settings_path(temp_data_dir)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('{"general": {"theme": "light"', encoding="utf-8")

        cfg = Config()
        backups = _invalid_backups(temp_data_dir)
        assert len(backups) == 1
        assert cfg.get("general.theme") == "dark"
        assert _raw(temp_data_dir)["general"]["theme"] == "dark"

    def test_root_not_object_backs_up_and_rebuilds(self, temp_data_dir):
        from src.core.config import Config

        _write(temp_data_dir, ["not", "a", "mapping"])
        cfg = Config()
        assert len(_invalid_backups(temp_data_dir)) == 1
        assert cfg.get("general.theme") == "dark"

    def test_future_schema_version_is_adopted_not_rebuilt(self, temp_data_dir):
        from src.core.config import SETTINGS_SCHEMA_VERSION, Config

        _write(
            temp_data_dir,
            {
                "schema_version": SETTINGS_SCHEMA_VERSION + 1,
                "general": {"theme": "light"},
            },
        )
        cfg = Config()
        assert not _invalid_backups(temp_data_dir)
        assert cfg.get("general.theme") == "light"
        # 读取不改文件；下一次写入磁盘时才按本程序补记版本号（只写不读）
        assert _raw(temp_data_dir)["schema_version"] == SETTINGS_SCHEMA_VERSION + 1
        cfg.set("general.theme", "dark")
        assert _raw(temp_data_dir)["schema_version"] == SETTINGS_SCHEMA_VERSION

    def test_non_int_schema_version_is_adopted_not_rebuilt(self, temp_data_dir):
        from src.core.config import Config

        _write(temp_data_dir, {"schema_version": "v1", "general": {"theme": "light"}})
        cfg = Config()
        assert not _invalid_backups(temp_data_dir)
        assert cfg.get("general.theme") == "light"

    def test_unknown_keys_survive_a_read_write_round_trip(self, temp_data_dir):
        from src.core.config import Config

        _write(
            temp_data_dir,
            {
                "schema_version": 999,
                "general": {"theme": "light", "future_option": True},
                "future_section": {"a": 1},
            },
        )
        cfg = Config()
        cfg.set("general.theme", "dark")
        raw = _raw(temp_data_dir)
        assert raw["future_section"] == {"a": 1}
        assert raw["general"]["future_option"] is True
        assert raw["general"]["theme"] == "dark"

    def test_reset_restores_defaults(self, temp_data_dir):
        from src.core.config import Config

        cfg = Config()
        cfg.set("general.theme", "light")
        cfg.reset()
        assert cfg.get("general.theme") == "dark"


class TestProxyLegacyRemoved:
    """代理设置由 proxy_config 结构提供，独立字符串字段不参与配置读写。"""

    def test_legacy_proxy_string_no_longer_activates_proxy(self, temp_data_dir):
        from src.core.config import Config
        from src.utils.proxy import get_active_proxy_url, get_configured_proxy_url

        _write(
            temp_data_dir,
            {
                "schema_version": 1,
                "gamebanana": {"proxy": "http://10.0.0.1:1080"},
            },
        )
        cfg = Config()
        assert get_configured_proxy_url(cfg) is None
        assert get_active_proxy_url(cfg) is None

    def test_manual_proxy_read_from_proxy_config(self, temp_data_dir):
        from src.core.config import Config
        from src.utils.proxy import get_active_proxy_url, get_configured_proxy_url

        _write(
            temp_data_dir,
            {
                "schema_version": 1,
                "gamebanana": {
                    "proxy_config": {
                        "active": True,
                        "enabled": True,
                        "mode": "manual",
                        "type": "socks5",
                        "host": "127.0.0.1",
                        "port": "1080",
                    }
                },
            },
        )
        cfg = Config()
        assert get_configured_proxy_url(cfg) == "socks5://127.0.0.1:1080"
        assert get_active_proxy_url(cfg) == "socks5://127.0.0.1:1080"

    def test_store_proxy_requires_enabled_flag(self, temp_data_dir):
        from src.core.config import Config
        from src.utils.proxy import get_active_proxy_url, get_configured_proxy_url

        _write(
            temp_data_dir,
            {
                "schema_version": 1,
                "gamebanana": {
                    "proxy_config": {
                        "active": True,
                        "enabled": False,
                        "mode": "manual",
                        "type": "http",
                        "host": "127.0.0.1",
                        "port": "7890",
                    }
                },
            },
        )
        cfg = Config()
        assert get_configured_proxy_url(cfg) == "http://127.0.0.1:7890"
        assert get_active_proxy_url(cfg) is None

    def test_master_switch_off_disables_everything(self, temp_data_dir):
        from src.core.config import Config
        from src.utils.proxy import get_active_proxy_url, get_configured_proxy_url

        _write(
            temp_data_dir,
            {
                "schema_version": 1,
                "gamebanana": {
                    "proxy_config": {
                        "active": False,
                        "enabled": True,
                        "mode": "manual",
                        "type": "http",
                        "host": "127.0.0.1",
                        "port": "7890",
                    }
                },
            },
        )
        cfg = Config()
        assert get_configured_proxy_url(cfg) is None
        assert get_active_proxy_url(cfg) is None

    def test_retired_proxy_mode_none_falls_back_to_system(self, temp_data_dir, monkeypatch):
        import src.utils.proxy as proxy_mod
        from src.core.config import Config

        _write(
            temp_data_dir,
            {
                "schema_version": 1,
                "gamebanana": {"proxy_config": {"active": True, "enabled": True, "mode": "none"}},
            },
        )
        cfg = Config()
        assert cfg.get("gamebanana.proxy_config.mode") == "system"
        monkeypatch.setattr(proxy_mod, "get_system_proxy", lambda: "http://sys:1")
        assert proxy_mod.get_active_proxy_url(cfg) == "http://sys:1"


@pytest.mark.parametrize(
    "key",
    [
        "general.theme",
        "general.language",
        "import.format",
        "import.strict_format",
        "import.parallel_workers",
        "mods.delete_method",
        "export.format",
        "export.strict_format",
        "export.preserve_tree",
        "export.include_metadata",
        "gamebanana.enabled",
        "gamebanana.nsfw_policy",
        "gamebanana.proxy_config.active",
        "gamebanana.proxy_config.enabled",
        "gamebanana.proxy_config.mode",
        "gamebanana.update_check.enabled",
        "gamebanana.update_check.min_recheck_hours",
        "app_update.on_startup",
        "app_update.download_source",
        "ui.adult_preview_mode",
        "ui.window_width",
    ],
)
def test_first_party_keys_resolve_without_caller_default(temp_data_dir, key):
    from src.core.config import Config

    assert Config().get(key) is not None
