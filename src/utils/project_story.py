"""关于页与构建流程共用的可替换短片元数据。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

from src.utils import paths
from src.utils.language_pack import N_

DEFAULT_TITLE = N_("火种未熄")
DEFAULT_SUBTITLE = N_("了解 Plus 背后的故事")
DEFAULT_EDITION = N_("2026 年 10 月项目短片")


@dataclass(frozen=True)
class ProjectStory:
    """项目介绍影片的媒体路径和展示文案。"""

    video_path: Path
    cover_path: Path
    title: str = DEFAULT_TITLE
    subtitle: str = DEFAULT_SUBTITLE
    edition: str = DEFAULT_EDITION


def get_project_story_dir() -> Path:
    """返回当前安装目录内的项目介绍资源位置。"""
    return paths.get_install_dir() / "resources" / "about" / "story"


def _media_path(directory: Path, value: object, default: str) -> Path:
    """清单中的路径仅能指向当前资源目录内的本地文件。"""
    if isinstance(value, str) and value.strip():
        relative = PureWindowsPath(value.strip())
        if not relative.anchor and ".." not in relative.parts and ":" not in value:
            candidate = directory.joinpath(*relative.parts)
            try:
                if candidate != directory and candidate.resolve().is_relative_to(directory.resolve()):
                    return candidate
            except (OSError, ValueError):
                pass
    return directory / default


def load_project_story(directory: Path | None = None) -> ProjectStory:
    """每次重新读取配置，使媒体替换无需重新构建。"""
    directory = Path(directory) if directory is not None else get_project_story_dir()
    try:
        manifest = json.loads((directory / "story.json").read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, ValueError):
        manifest = {}
    if not isinstance(manifest, dict):
        manifest = {}

    def text(key: str, default: str) -> str:
        """读取清单中的非空字符串，缺失或格式无效时返回默认文案。"""
        value = manifest.get(key)
        return value.strip() if isinstance(value, str) and value.strip() else default

    return ProjectStory(
        video_path=_media_path(directory, manifest.get("video"), "film.mp4"),
        cover_path=_media_path(directory, manifest.get("cover"), "cover.png"),
        title=text("title", DEFAULT_TITLE),
        subtitle=text("subtitle", DEFAULT_SUBTITLE),
        edition=text("edition", DEFAULT_EDITION),
    )
