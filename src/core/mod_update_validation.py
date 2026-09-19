"""替换已有 Mod 前完整读取更新内容。"""

from __future__ import annotations

import zipfile
from pathlib import Path

from src.services import archive as archive_backend
from src.utils import fs
from src.utils.i18n import t as tr


class UpdateArchiveValidationError(ValueError):
    """可用后端无法完整解码并验证更新。"""


def _validate_7z(path: Path) -> None:
    """用 py7zr 完整解码压缩成员并丢弃输出，以验证数据完整性；缺少后端时抛出错误。"""
    if not archive_backend.PY7ZR_AVAILABLE:
        raise RuntimeError(tr("缺少可用的 7z 校验后端（py7zr）"))
    with archive_backend.py7zr.SevenZipFile(fs.native_path(path), mode="r") as archive:
        from py7zr.io import NullIOFactory

        # 完整解码并校验成员及数据流，不创建解压文件或保留内容缓冲。
        archive.extractall(factory=NullIOFactory())


def _validate_rar(path: Path, archive_service) -> str | None:
    """打开可用 RAR 后端执行完整性测试，返回后端结果并确保关闭句柄。"""
    archive, _kind = archive_service._rar_open(path)
    if archive is None:
        raise RuntimeError(tr("缺少可用的 RAR 校验后端"))
    try:
        # 沿用解压时的后端选择，完整校验成员数据而不写入解压文件。
        return archive.testrar()
    finally:
        close = getattr(archive, "close", None)
        if close is not None:
            close()


def validate_update_archive(path: str | Path, archive_service) -> None:
    """完整校验受支持的更新归档，失败时返回可读错误。

    目录列表或内容身份摘要不能替代完整性检查；不修改源归档或已有文件库。
    """
    archive = Path(path)
    try:
        if not fs.is_file(archive):
            raise FileNotFoundError(tr("文件不存在或无法读取"))
        if not archive_service.is_supported(archive):
            raise ValueError(tr("不支持的压缩格式：{value}", value=archive.suffix or tr("无扩展名")))
        suffix = archive.suffix.lower()
        bad_member = None
        if suffix == ".zip":
            with zipfile.ZipFile(fs.native_path(archive), "r") as source:
                # 逐个使用 ZipInfo，避免同名成员被映射到最后一项而漏检。
                for member in source.infolist():
                    with source.open(member, "r") as payload:
                        while payload.read(1024 * 1024):
                            pass  # 读取到文件末尾时由 ZipExtFile 校验 CRC。
        elif suffix == ".7z":
            _validate_7z(archive)
        elif suffix == ".rar":
            bad_member = _validate_rar(archive, archive_service)
        else:
            raise ValueError(tr("没有可用的完整性校验后端：{suffix}", suffix=suffix))
        if bad_member is not None:
            raise ValueError(tr("成员数据损坏或 CRC 校验不一致：{bad_member}", bad_member=bad_member))
    except Exception as error:
        detail = str(error) or type(error).__name__
        raise UpdateArchiveValidationError(
            tr("更新压缩包校验失败（{name}）：{detail}", name=archive.name, detail=detail)
        ) from error
