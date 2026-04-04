"""GameBanana 接口数据模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Self


class GbModel(str, Enum):
    """标识在线服务支持的 Mod、制作中内容及工具资源类型。"""

    MOD = "Mod"
    WIP = "Wip"
    TOOL = "Tool"

    @classmethod
    def coerce(cls, raw: str | None) -> GbModel:
        """接受规范或不区分大小写的资源类型名称，缺失或未知值按 Mod 处理。"""
        if not raw:
            return cls.MOD
        try:
            return cls(raw)
        except ValueError:
            r = raw.lower()
            return {"mod": cls.MOD, "wip": cls.WIP, "tool": cls.TOOL}.get(r, cls.MOD)


def _i(payload: dict, key: str, default: int = 0) -> int:
    """将响应字段转换为整数，缺失或转换失败时使用调用方提供的默认值。"""
    v = payload.get(key)
    try:
        return int(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def _s(payload: dict, key: str, default: str = "") -> str:
    """将存在的响应字段转换为字符串，缺失字段使用默认文本。"""
    v = payload.get(key)
    return str(v) if v is not None else default


def _is_nsfw(payload: dict) -> bool:
    """根据站点可见性和内容评级判断敏感内容。

    初始可见性 show 表示直接显示，hide 或 warn 表示需隐藏或提示。
    内容评级标志提供补充判断依据。
    """
    vis = (payload.get("_sInitialVisibility") or "").lower()
    if vis in ("hide", "warn"):
        return True
    # 同时读取接口提供的敏感内容标志。
    if payload.get("_bIsNsfw") or payload.get("_bContainsNsfwContent"):
        return True
    return False


@dataclass(frozen=True)
class GbAuthor:
    """保存在线作者的身份、显示名称、头像和个人页面地址。"""

    id: int
    name: str
    avatar_url: str
    profile_url: str

    @classmethod
    def from_api(cls, payload: dict) -> Self:
        """从在线作者对象提取身份与展示字段，缺失字段使用类型对应的默认值。"""
        return cls(
            id=_i(payload, "_idRow"),
            name=_s(payload, "_sName"),
            avatar_url=_s(payload, "_sAvatarUrl"),
            profile_url=_s(payload, "_sProfileUrl"),
        )


@dataclass(frozen=True)
class GbModFile:
    """描述可下载文件的身份、地址、大小、版本及服务端校验和安全扫描信息。"""

    file_id: int
    file_name: str
    description: str
    download_url: str
    size_bytes: int
    date_added_ts: int
    md5_checksum: str
    av_result: str
    version: str = ""
    # 文件级 _nDownloadCount 用于详情中的下载量显示。
    download_count: int = 0

    @classmethod
    def from_api(cls, payload: dict) -> Self:
        """将下载文件响应转换为模型，保留文件时间戳、摘要和下载量供安装与更新比较。"""
        return cls(
            file_id=_i(payload, "_idRow"),
            file_name=_s(payload, "_sFile"),
            description=_s(payload, "_sDescription"),
            download_url=_s(payload, "_sDownloadUrl"),
            size_bytes=_i(payload, "_nFilesize"),
            date_added_ts=_i(payload, "_tsDateAdded"),
            md5_checksum=_s(payload, "_sMd5Checksum"),
            av_result=_s(payload, "_sAvResult"),
            version=_s(payload, "_sVersion"),
            download_count=_i(payload, "_nDownloadCount"),
        )


@dataclass(frozen=True)
class GbCategory:
    """保存在线分类标识、父分类关系、条目数量和可选图标。"""

    id: int
    name: str
    parent_id: int | None
    item_count: int
    icon_url: str | None

    @classmethod
    def from_api(cls, payload: dict) -> Self:
        """解析分类信息，接受接口提供的两种父分类字段，根分类使用空父标识。"""
        parent = payload.get("_idParentCategoryRow") or payload.get("_idParentRow")
        return cls(
            id=_i(payload, "_idRow"),
            name=_s(payload, "_sName"),
            parent_id=int(parent) if parent else None,
            item_count=_i(payload, "_nItemCount"),
            icon_url=_s(payload, "_sIconUrl") or None,
        )


@dataclass(frozen=True)
class GbModUpdateEntry:
    """保存更新日志的正文、版本、标签及受影响文件名称。"""

    title: str
    date_added_ts: int
    description_html: str
    tags: list[str]
    # 版本号（apiv11 _sVersion，"v1.2" / "1.0" 等任意字符串）。
    version: str = ""
    # 更新涉及的文件名列表来自 _aFiles[*]._sFile，供用户判断安装文件是否受影响。
    affected_files: list[str] = field(default_factory=list)

    @classmethod
    def from_api(cls, payload: dict) -> Self:
        """转换更新日志记录，并从有效文件条目中提取非空名称。"""
        files = payload.get("_aFiles") or []
        affected: list[str] = []
        if isinstance(files, list):
            for f in files:
                if isinstance(f, dict):
                    name = _s(f, "_sFile")
                    if name:
                        affected.append(name)
        return cls(
            title=_s(payload, "_sTitle") or _s(payload, "_sName"),
            date_added_ts=_i(payload, "_tsDateAdded"),
            description_html=_s(payload, "_sText"),
            tags=list(payload.get("_aTags") or []),
            version=_s(payload, "_sVersion"),
            affected_files=affected,
        )


def _extract_thumbnail(payload: dict) -> str:
    """选择适合卡片显示的缩略图 URL。

    优先使用宽度为 530 像素的变体，满足约 210 逻辑像素卡片在高 DPI 屏幕上的清晰度。
    缺失时按可用尺寸回退。
    """
    media = payload.get("_aPreviewMedia") or {}
    images = media.get("_aImages") or []
    if not images:
        return ""
    first = images[0]
    base = first.get("_sBaseUrl", "")
    thumb = first.get("_sFile530") or first.get("_sFile220") or first.get("_sFile800") or first.get("_sFile") or ""
    if base and thumb:
        return f"{base}/{thumb}"
    return ""


def _extract_preview_images(payload: dict) -> list[str]:
    """按响应顺序组合预览图片的基础地址与文件名，跳过缺少任一部分的条目。"""
    media = payload.get("_aPreviewMedia") or {}
    images = media.get("_aImages") or []
    out: list[str] = []
    for img in images:
        base = img.get("_sBaseUrl", "")
        f = img.get("_sFile") or ""
        if base and f:
            out.append(f"{base}/{f}")
    return out


@dataclass
class GbModRecord:
    """保存在线列表卡片需要的资源身份、作者、统计及内容分级信息。"""

    mod_id: int
    model: GbModel
    name: str
    submitter: GbAuthor
    thumbnail_url: str
    page_url: str
    view_count: int
    like_count: int
    download_count: int
    comment_count: int
    is_nsfw: bool
    date_updated_ts: int

    @classmethod
    def from_api(cls, payload: dict) -> Self:
        """将列表响应转换为资源记录，更新时间缺失时使用发布时间。"""
        return cls(
            mod_id=_i(payload, "_idRow"),
            model=GbModel.coerce(_s(payload, "_sModelName")),
            name=_s(payload, "_sName"),
            submitter=GbAuthor.from_api(payload.get("_aSubmitter") or {}),
            thumbnail_url=_extract_thumbnail(payload),
            page_url=_s(payload, "_sProfileUrl"),
            view_count=_i(payload, "_nViewCount"),
            like_count=_i(payload, "_nLikeCount"),
            download_count=_i(payload, "_nDownloadCount"),
            comment_count=_i(payload, "_nPostCount"),
            is_nsfw=_is_nsfw(payload),
            date_updated_ts=_i(payload, "_tsDateUpdated") or _i(payload, "_tsDateAdded"),
        )


@dataclass
class GbModProfile(GbModRecord):
    """在列表记录基础上保存资源正文、预览、可下载文件、分类关系和更新提示。"""

    description_html: str = ""
    preview_images: list[str] = field(default_factory=list)
    files: list[GbModFile] = field(default_factory=list)
    category_id: int = 0
    category_name: str = ""
    # 父分类来自 ProfilePage 顶层的 _aSuperCategory，不位于 _aCategory 内。
    # 分类名用于匹配角色目录；super_category_id 为零时表示根分区，
    # 此时 category_name 不能用于自动选择角色目录。
    super_category_id: int = 0
    super_category_name: str = ""
    updates: list[GbModUpdateEntry] = field(default_factory=list)
    # ProfilePage 只返回更新标志和数量，完整记录须从 /Updates 获取。
    # has_updates 决定更新标签是否显示，updates_count 用于记录接口状态。
    has_updates: bool = False
    updates_count: int = 0

    @classmethod
    def from_api(cls, payload: dict) -> Self:
        """解析详情响应及其嵌套模型，正文缺失时使用摘要，并分别保留分类与顶层父分类。"""
        base_kwargs = dict(
            mod_id=_i(payload, "_idRow"),
            model=GbModel.coerce(_s(payload, "_sModelName")),
            name=_s(payload, "_sName"),
            submitter=GbAuthor.from_api(payload.get("_aSubmitter") or {}),
            thumbnail_url=_extract_thumbnail(payload),
            page_url=_s(payload, "_sProfileUrl"),
            view_count=_i(payload, "_nViewCount"),
            like_count=_i(payload, "_nLikeCount"),
            download_count=_i(payload, "_nDownloadCount"),
            comment_count=_i(payload, "_nPostCount"),
            is_nsfw=_is_nsfw(payload),
            date_updated_ts=_i(payload, "_tsDateUpdated") or _i(payload, "_tsDateAdded"),
        )
        cat = payload.get("_aCategory") or {}
        sup = payload.get("_aSuperCategory") or {}
        # 优先使用完整正文 _sText，仅在正文缺失时回退到摘要 _sDescription。
        return cls(
            **base_kwargs,
            description_html=_s(payload, "_sText") or _s(payload, "_sDescription"),
            preview_images=_extract_preview_images(payload),
            files=[GbModFile.from_api(f) for f in (payload.get("_aFiles") or [])],
            category_id=_i(cat, "_idRow"),
            category_name=_s(cat, "_sName"),
            super_category_id=_i(sup, "_idRow"),
            super_category_name=_s(sup, "_sName"),
            updates=[GbModUpdateEntry.from_api(u) for u in (payload.get("_aUpdates") or [])],
            has_updates=bool(payload.get("_bHasUpdates")),
            updates_count=_i(payload, "_nUpdatesCount"),
        )


@dataclass
class GbInstallMetadata:
    """记录本地安装文件对应的在线来源、版本、作者和最近更新检查时间。"""

    mod_id: int
    file_id: int
    file_md5: str
    file_ts: int
    page_url: str
    version: str | None
    submitter_id: int
    submitter_name: str
    last_checked_iso: str

    def to_dict(self) -> dict:
        """将安装来源和版本证据转换为可持久化的字段字典。"""
        return {
            "mod_id": self.mod_id,
            "file_id": self.file_id,
            "file_md5": self.file_md5,
            "file_ts": self.file_ts,
            "page_url": self.page_url,
            "version": self.version,
            "submitter_id": self.submitter_id,
            "submitter_name": self.submitter_name,
            "last_checked_iso": self.last_checked_iso,
        }

    @classmethod
    def from_dict(cls, d: dict) -> Self:
        """从持久化字段恢复安装元数据，缺项使用默认值，非法整数值由转换异常报告。"""
        return cls(
            mod_id=int(d.get("mod_id", 0)),
            file_id=int(d.get("file_id", 0)),
            file_md5=str(d.get("file_md5", "")),
            file_ts=int(d.get("file_ts", 0)),
            page_url=str(d.get("page_url", "")),
            version=d.get("version"),
            submitter_id=int(d.get("submitter_id", 0)),
            submitter_name=str(d.get("submitter_name", "")),
            last_checked_iso=str(d.get("last_checked_iso", "")),
        )
