"""从独立快照生成并原子写入清单，计算和文件操作不依赖 Qt。"""

from __future__ import annotations

import csv
import io
import os
import tempfile
from pathlib import Path

from src.plugin_sdk import TaskContext
from src.plugin_sdk.facades import CharacterInfo, ModInfo

_FIELDS = ("game_id", "mod_id", "category", "name", "author", "enabled", "rating", "tags")


def _node_chain(node_id: str, nodes: dict[str, CharacterInfo]) -> list[str]:
    """沿父节点收集分类身份，保留缺失节点并在重复身份处停止。"""
    chain = []
    seen = set()
    while node_id and node_id not in seen:
        chain.append(node_id)
        seen.add(node_id)
        node = nodes.get(node_id)
        node_id = node.parent_id if node is not None else ""
    return chain


def _text_cell(value: str) -> str:
    """为可能被电子表格解释为公式的单元格添加文本前缀。"""
    if value.lstrip().startswith(("=", "+", "-", "@")) or value.startswith(("\t", "\r", "\n")):
        return "'" + value
    return value


def build_inventory(
    mods: list[ModInfo],
    characters: list[CharacterInfo],
    *,
    node_id: str = "",
    enabled_only: bool = False,
    task: TaskContext | None = None,
) -> str:
    """把快照转换为 CSV，按分类子树筛选，并在处理批次之间响应取消。"""
    nodes = {node.id: node for node in characters}
    rows = []
    for index, mod in enumerate(mods):
        if task is not None and index % 128 == 0:
            task.report_progress(0.8 * index / max(1, len(mods)))
        if enabled_only and not mod.enabled:
            continue
        chain = _node_chain(mod.node_id, nodes)
        if node_id and node_id not in chain:
            continue
        category = " / ".join(nodes[ident].name if ident in nodes else ident for ident in reversed(chain))
        rows.append(
            (
                mod.game_id,
                mod.mod_id,
                category,
                mod.name,
                mod.author,
                str(mod.enabled).lower(),
                mod.rating,
                "; ".join(mod.tags),
            )
        )
    if task is not None:
        task.raise_if_cancelled()
    rows.sort(key=lambda row: (row[2].casefold(), row[3].casefold(), row[1]))
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(_FIELDS)
    for index, row in enumerate(rows):
        if task is not None and index % 128 == 0:
            task.report_progress(0.8 + 0.15 * index / max(1, len(rows)))
        writer.writerow(tuple(_text_cell(value) for value in row))
    return stream.getvalue()


def write_inventory(path: str, content: str, *, task: TaskContext | None = None) -> None:
    """以带 BOM 的 UTF-8 原子替换目标文件，提交前取消或写入失败会保留原文件。"""
    target = Path(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=".mod-inventory-", suffix=".tmp", dir=target.parent, delete=False
        ) as output:
            temporary = Path(output.name)
            output.write(content.encode("utf-8-sig"))
            output.flush()
            os.fsync(output.fileno())
        if task is not None:
            task.raise_if_cancelled()
        temporary.replace(target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def export_inventory(
    task: TaskContext,
    *,
    mods: list[ModInfo],
    characters: list[CharacterInfo],
    path: str,
    node_id: str,
    enabled_only: bool,
) -> str:
    """在后台完成纯数据计算及文件提交，返回已导出的路径。"""
    content = build_inventory(mods, characters, node_id=node_id, enabled_only=enabled_only, task=task)
    task.report_progress(0.95)
    write_inventory(path, content, task=task)
    return path
