import json

import pytest

from src.core.config import Config


def test_missing_memory_preferences_default_without_rewriting_file(temp_data_dir):
    config = Config()
    config.set("general.theme", "light")
    before = config._config_path.read_bytes()
    config.reload()
    assert config.get("images.memory_cache_mode") == "auto"
    assert config.get("images.memory_cache_mb") == 768
    assert config._config_path.read_bytes() == before


@pytest.mark.parametrize("bad", [0, -1, True, "512", 63, 8193, 512.5])
def test_bad_manual_budget_falls_back_only_that_key(temp_data_dir, bad):
    config = Config()
    config._config_path.write_text(
        json.dumps(
            {
                "general": {"theme": "light"},
                "images": {"memory_cache_mode": "manual", "memory_cache_mb": bad},
            }
        ),
        encoding="utf-8",
    )
    config.reload()
    assert config.get("images.memory_cache_mb") == 768
    assert config.get("images.memory_cache_mode") == "manual"
    assert config.get("general.theme") == "light"


def test_invalid_mode_keeps_valid_manual_preference(temp_data_dir):
    config = Config()
    config.set("images", {"memory_cache_mode": "unlimited", "memory_cache_mb": 1024})
    config.reload()
    assert config.get("images.memory_cache_mode") == "auto"
    assert config.get("images.memory_cache_mb") == 1024


def test_recommendation_is_bounded_by_ram_and_accounts_for_display_workload():
    from src.core.image_memory_policy import recommend_image_cache_mb

    assert recommend_image_cache_mb(None, width=3840, height=2160, dpr=2) == 256
    small = recommend_image_cache_mb(32 * 1024**3, width=1280, height=720)
    large = recommend_image_cache_mb(32 * 1024**3, width=3840, height=2160)
    assert 64 <= small < large <= 1024
    assert recommend_image_cache_mb(8 * 1024**3, width=3840, height=2160) <= 256
    assert recommend_image_cache_mb(32 * 1024**3, width=1920, height=1080, dpr=2) >= small
    assert recommend_image_cache_mb(0, width=0, height=0, dpr=0) == 256
