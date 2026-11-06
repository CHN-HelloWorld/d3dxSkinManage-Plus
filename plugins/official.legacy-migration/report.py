"""格式化迁移结果；文件写入和默认应用打开统一交给宿主。"""

from __future__ import annotations

from datetime import datetime


def migration_report(controller) -> str:
    """汇总重复项、实际目标身份、来源选择和全部扫描/复制诊断。"""
    tr = controller.ctx.i18n.t

    def field(label, value):
        """把不受信任的换行转为可见文本，避免伪造报告字段。"""
        text = str(value).replace("\r", "\\r").replace("\n", "\\n")
        return f"{tr(label)}: {text}"

    summary = controller.summary
    lines = [
        tr("旧版迁移报告"),
        field("生成时间", datetime.now().isoformat(timespec="seconds")),
        field("目标游戏", controller.target_game_name),
        field("目标游戏 ID", controller.target_game_id),
        field("旧版目录", controller.root),
        field("旧版用户环境", controller.scan.environment.name),
        "",
        tr("复制保留旧版文件；新导入的 Mod 保持停用。"),
        tr(
            "成功 {succeeded} · 跳过 {skipped} · 失败 {failed} · 取消 {cancelled}",
            succeeded=summary.succeeded,
            skipped=summary.skipped,
            failed=summary.failed,
            cancelled=summary.cancelled,
        ),
    ]
    headings = {
        "skipped": "重复 Mod（已跳过）",
        "succeeded": "成功导入",
        "failed": "导入失败",
        "cancelled": "已取消",
        "pending": "尚未复制",
        "running": "正在复制",
    }
    for status, heading in headings.items():
        items = [item for item in controller.items if item.status == status]
        if not items:
            continue
        lines.extend(("", tr(heading), "=" * 48))
        for number, item in enumerate(items, 1):
            source = item.source
            use_cache = bool(source.cache_path and source.source_path == source.cache_path)
            lines.extend(
                (
                    f"{number}. {source.name.replace(chr(10), ' ')}",
                    field("来源", source.source_path),
                    field("来源类型", tr("解压缓存") if use_cache else tr("原包")),
                    field("旧版索引键", source.legacy_key or source.sha),
                    field("原包 SHA-1", source.sha or tr("无")),
                    field("目标 Mod ID", item.target_id or tr("无")),
                    field("目标分类", " / ".join(controller.destination_path(source))),
                )
            )
            if use_cache:
                lines.append(field("缓存指纹", source.cache_digest))
            if status == "skipped":
                lines.append(field("原因", tr("目标游戏已有相同 Mod，保留现有数据，不重复导入。")))
            if item.error:
                lines.append(field("详情", item.error))
            lines.extend(field("提示", warning) for warning in item.warnings)
            lines.append("")
    lines.extend(("", tr("扫描与复制问题"), "=" * 48))
    for number, issue in enumerate(controller.issues, 1):
        lines.extend(
            (
                f"{number}. {tr(issue.message, name=issue.name, source=issue.source)}",
                field("条目", issue.name),
                field("来源", issue.source),
            )
        )
    if not controller.issues:
        lines.append(tr("无"))
    return "\n".join(lines) + "\n"
