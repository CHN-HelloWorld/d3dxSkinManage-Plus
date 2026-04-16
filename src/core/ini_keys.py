"""解析 3DMigoto INI 按键位置并安全修改对应行，模块不依赖 Qt。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from src.core.key_tokens import compatible_tail_flags, split_key_expression
from src.utils import fs
from src.utils.i18n import t as tr

_SECTION_RE = re.compile(r"^\s*\[(?P<name>[^\]]+)\]\s*$")
# field 限定 key/back，且 = 紧跟（容忍空格），避免误匹配 keyword= / key_x=
_KV_RE = re.compile(r"(?i)^\s*(?P<field>key|back)\s*=\s*(?P<rhs>.*?)\s*(?:;.*)?$")
_TYPE_RE = re.compile(r"(?i)^\s*type\s*=\s*(?P<rhs>.*?)\s*(?:;.*)?$")
_COND_RE = re.compile(r"(?i)^\s*condition\s*=\s*(?P<rhs>.*?)\s*(?:;.*)?$")
_VAR_RE = re.compile(r"^\s*(?P<name>\$\w+)\s*=\s*(?P<rhs>.*?)\s*(?:;.*)?$")


@dataclass
class KeyBinding:
    """保存 INI 按键段的正反向按键、源行号、变量和循环条件。"""

    ini_path: str  # 写回定位用（parse_mod_keys 下为绝对路径）
    section: str  # 如 "KeySwap"
    key_line_no: int  # key= 所在行（0-based）
    key_value: str  # 键值/键表达式（已剥离尾 flag 与注释）
    key_flags: str = ""  # no_* 尾 flag，写回时原样接回
    back_line_no: int | None = None
    back_value: str | None = None
    back_flags: str = ""
    var_name: str | None = None  # $swapvar 等（只读展示）
    cycle_values: str | None = None  # "0,1,2,3"（只读展示）
    type: str | None = None  # "cycle" 等（只读展示）
    condition: str | None = None  # 只读展示
    rel: str = ""  # ini 在本体内的相对路径（override 写回路由用；
    #   归档本体无真实文件路径，靠 rel + mod 定位 override）


def parse_ini_text(text: str, ini_path: str) -> list[KeyBinding]:
    """逐行解析一个 ini 文本，返回其中所有含 key= 的 section 绑定。"""
    bindings: list[KeyBinding] = []
    pending: dict | None = None

    def _flush() -> None:
        """将包含主按键行的待解析段转换为绑定对象，并清空当前段状态。"""
        nonlocal pending
        if pending is not None and "key_line_no" in pending:
            bindings.append(
                KeyBinding(
                    ini_path=ini_path,
                    section=pending["section"],
                    key_line_no=pending["key_line_no"],
                    key_value=pending["key_value"],
                    key_flags=pending.get("key_flags", ""),
                    back_line_no=pending.get("back_line_no"),
                    back_value=pending.get("back_value"),
                    back_flags=pending.get("back_flags", ""),
                    var_name=pending.get("var_name"),
                    cycle_values=pending.get("cycle_values"),
                    type=pending.get("type"),
                    condition=pending.get("condition"),
                )
            )
        pending = None

    for i, line in enumerate(text.splitlines()):
        ms = _SECTION_RE.match(line)
        if ms:
            _flush()
            pending = {"section": ms.group("name").strip()}
            continue
        if pending is None:
            continue
        mk = _KV_RE.match(line)
        if mk:
            disp, tail = split_key_expression(mk.group("rhs").strip())
            if mk.group("field").lower() == "key":
                pending["key_line_no"] = i
                pending["key_value"] = disp
                pending["key_flags"] = tail
            else:
                pending["back_line_no"] = i
                pending["back_value"] = disp
                pending["back_flags"] = tail
            continue
        mt = _TYPE_RE.match(line)
        if mt:
            pending["type"] = mt.group("rhs").strip()
            continue
        mc = _COND_RE.match(line)
        if mc:
            pending["condition"] = mc.group("rhs").strip()
            continue
        mv = _VAR_RE.match(line)
        if mv:
            pending["var_name"] = mv.group("name")
            pending["cycle_values"] = mv.group("rhs").strip()
            continue
    _flush()
    return bindings


def parse_mod_keys(mod_path: str) -> list[KeyBinding]:
    """遍历 Mod 文件夹下所有 .ini，解析全部按键绑定。"""
    result: list[KeyBinding] = []
    base = Path(fs.normal_path(mod_path))
    if not fs.is_dir(base):
        return result
    try:
        ini_files = [path for path in fs.iter_files(base) if path.suffix.lower() == ".ini"]
    except Exception:
        return result

    def _depth_then_name(p: Path):
        # 按目录深度优先读取外层 INI，同层按名称排序且忽略大小写。
        """生成按相对目录深度及不区分大小写名称排序的键。"""
        try:
            parts = p.relative_to(base).parts
        except ValueError:
            parts = p.parts
        return (len(parts),) + tuple(part.lower() for part in parts)

    for ini in sorted(ini_files, key=_depth_then_name):
        try:
            with open(fs.native_path(ini), encoding="utf-8", errors="ignore") as fh:
                text = fh.read()
        except Exception:
            continue
        result.extend(parse_ini_text(text, str(ini)))
    return result


def extract_key_names(bindings: list[KeyBinding]) -> list[str]:
    """从绑定列表收集去重后的键名（保持出现顺序），供导入扫描等只需键名的场景。"""
    seen: set[str] = set()
    out: list[str] = []
    for b in bindings:
        for v in (b.key_value, b.back_value):
            if v and v not in seen:
                seen.add(v)
                out.append(v)
    return out


# 行级写回


class IniWriteError(Exception):
    """写回 ini 失败（定位不到目标行 / 文件被占用 / 行已变更等）。"""


_WRITE_RE = re.compile(r"(?i)^(?P<pre>\s*(?P<field>key|back)\s*=\s*)(?P<rhs>.*?)(?P<comment>\s*;.*)?$")


def rewrite_key_in_text(content: str, line_no: int, field: Literal["key", "back"], new_token: str) -> str:
    """替换指定行的 key 或 back 值，保留缩进、标志、注释及其余内容。

    不访问磁盘，供文件写入和容器覆盖编辑共用。目标行不存在或内容已变化时抛出 IniWriteError。
    """
    lines = content.splitlines(keepends=True)
    if line_no < 0 or line_no >= len(lines):
        raise IniWriteError(tr("目标行已超出文件范围（文件可能被改动）"))

    raw = lines[line_no]
    eol = ""
    body = raw
    for nl in ("\r\n", "\n", "\r"):
        if body.endswith(nl):
            eol, body = nl, body[: -len(nl)]
            break

    m = _WRITE_RE.match(body)
    if not m or m.group("field").lower() != field:
        raise IniWriteError(tr("目标行已变更，拒绝写入（请重新打开详情）"))

    # 保留原 no_* 尾 flag
    _disp, tail = split_key_expression(m.group("rhs").strip())
    tail = compatible_tail_flags(new_token, tail)
    new_rhs = new_token if not tail else f"{new_token} {tail}"
    new_body = m.group("pre") + new_rhs + (m.group("comment") or "")

    lines[line_no] = new_body + eol
    return "".join(lines)
