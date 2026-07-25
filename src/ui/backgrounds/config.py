"""解析静态图片偏好和动态背景来源配置。"""

from src.core.config import Config


def background_options(config: Config) -> dict:
    """读取背景配置并规范化来源、帧率、场景缩放和音量；禁用时强制关闭来源。"""
    defaults = Config._DEFAULT_SETTINGS["appearance"]["background"]
    result = {key: config.get(f"appearance.background.{key}", value) for key, value in defaults.items()}
    if not result["enabled"]:
        result["source"] = "off"
    if result["source"] not in ("off", "image", "video", "scene"):
        result["source"] = "off"
    result["fps"] = 60 if result["fps"] == 60 else 30
    result["scene_scale"] = 1 if result["scene_scale"] == 1 else 2
    result["volume"] = max(0, min(100, int(result["volume"])))
    return result
