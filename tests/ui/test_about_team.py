from PySide6.QtGui import QImage

from src.ui.pages.about import _TEAM, _avatars_dir


def test_special_supporter_follows_existing_supporter():
    existing_index = next(index for index, member in enumerate(_TEAM) if member[1] == "菊理千姬")

    assert _TEAM[existing_index + 1] == (
        "为人民服务.jpg",
        "为人民服务",
        "特别支持",
        "持续关注 · 鼎力支持",
        None,
        False,
    )


def test_special_supporter_avatar_loads():
    assert not QImage(str(_avatars_dir() / "为人民服务.jpg")).isNull()
