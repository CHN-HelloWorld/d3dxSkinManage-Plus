def test_gamebanana_defaults(temp_data_dir):
    from src.core.config import Config

    cfg = Config()
    assert cfg.get("gamebanana.enabled") is True
    assert cfg.get("gamebanana.nsfw_policy") == "blur"
    assert cfg.get("gamebanana.max_concurrency") == 8


def test_gamebanana_set_then_get(temp_data_dir):
    from src.core.config import Config

    cfg = Config()
    cfg.set("gamebanana.nsfw_policy", "show")
    assert cfg.get("gamebanana.nsfw_policy") == "show"


def test_gamebanana_overrides_default(temp_data_dir):
    from src.core.game_config_store import GameConfigStore

    store = GameConfigStore()
    store.set_gamebanana_overrides({"game_a": 99999})
    assert store.get_gamebanana_overrides() == {"game_a": 99999}


def test_gamebanana_user_value_resolved_over_default(temp_data_dir):
    """只保存自定义值，其他配置读取内置默认值，不将默认值回写到文件。"""
    import json

    from src.core.config import Config

    # 配置文件仅含一个自定义值。
    config_dir = temp_data_dir / "config"
    settings_path = config_dir / "settings.json"
    settings_path.write_text(
        json.dumps(
            {
                "gamebanana": {"nsfw_policy": "show"},  # 自定义值。
            }
        ),
        encoding="utf-8",
    )

    cfg = Config()
    assert cfg.get("gamebanana.nsfw_policy") == "show"  # 保留自定义值。
    assert cfg.get("gamebanana.enabled") is True  # 读取时采用默认值。
    assert cfg.get("gamebanana.max_concurrency") == 8  # 读取时采用默认值。

    on_disk = json.loads(settings_path.read_text(encoding="utf-8"))
    assert on_disk["gamebanana"] == {"nsfw_policy": "show"}, "默认值不得写回用户文件"


def test_update_check_defaults(temp_data_dir):
    from src.core.config import Config

    cfg = Config()
    assert cfg.get("gamebanana.update_check.enabled") is True
    assert cfg.get("gamebanana.update_check.on_startup") is True
    assert cfg.get("gamebanana.update_check.periodic_hours") == 0
    assert cfg.get("gamebanana.update_check.min_recheck_hours") == 24
    assert cfg.get("gamebanana.update_check.notify_style") == "badge"


def test_update_check_defaults_for_existing_user(temp_data_dir):
    import json

    from src.core.config import Config

    settings_path = temp_data_dir / "config" / "settings.json"
    settings_path.write_text(json.dumps({"gamebanana": {"nsfw_policy": "show"}}), encoding="utf-8")
    cfg = Config()
    assert cfg.get("gamebanana.nsfw_policy") == "show"  # 原值保留
    assert cfg.get("gamebanana.update_check.min_recheck_hours") == 24  # 默认回退处理
