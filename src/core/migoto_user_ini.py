"""定位、稳定读取并局部改写 3DMigoto 的 d3dx_user.ini。

模块不依赖 Qt 或 Mod 库结构。文件格式和写回规则如下：

- 文件包含自动生成的文件头及单个 Constants 段，保存时整体覆盖。
- 变量名使用小写和反斜杠分隔，包含命名空间与变量标识。
- 数值最多保留九位有效数字，使用 values_equal 比较，避免将 2 与 2.0 视为变更。
- 不同 3DMigoto 分支使用不同编码；按 BOM、严格 UTF-8、系统 ANSI 顺序探测，
  写回时保持原编码。
- d3dx.ini 的 Include.user_config 可指定位置，相对路径基于模块目录解析。
- 游戏重载配置或退出时保存，重置快捷键会删除文件；具体读写时机由调用方决定。
"""

from __future__ import annotations

import hashlib
import re
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path

from src.utils import fs
from src.utils.i18n import t as tr
from src.utils.logger import logger

#: 上游默认文件名（d3dx.ini 未声明 ``[Include] user_config`` 时）。
DEFAULT_USER_CONFIG_NAME = "d3dx_user.ini"
#: 上游唯一会写出的段名。
CONSTANTS_SECTION = "Constants"

_SECTION_RE = re.compile(r"^\ufeff?\s*\[(?P<name>[^\]]+)\]")
# 持久化变量行：``$\ns\var = 2``（左侧必须以 $ 开头；右侧到行尾/注释）。
_PERSIST_LINE_RE = re.compile(r"^(?P<indent>\s*)(?P<key>\$[^=;]*?)\s*=\s*(?P<value>[^;]*?)(?P<trailer>\s*(?:;.*)?)$")
# d3dx.ini 里的 ``user_config = xxx``（``[Include]`` 段内）。
_USER_CONFIG_RE = re.compile(r"(?i)^\s*user_config\s*=\s*(?P<value>[^;]*?)\s*(?:;.*)?$")
# ``surrogateescape`` 透传解码留下的孤立代理码位（见 :func:`decode_passthrough_text`）。
_SURROGATE_RE = re.compile("[\ud800-\udfff]")


class UserIniUnstableError(RuntimeError):
    """文件在观察窗口内一直在变（游戏正在写）——本次不得据此更新偏好。"""


@dataclass(frozen=True)
class UserIniSnapshot:
    """一次成功的稳定读取结果。"""

    path: Path
    text: str
    sha256: str
    encoding: str
    #: 规范化键 -> 原始值字符串（保留上游写出的字面量，不做数值归一）
    values: dict[str, str] = field(default_factory=dict)


# 定位


def resolve_user_config_path(migoto_path: str | Path | None) -> Path | None:
    """定位 ``d3dx_user.ini``：读 d3dx.ini 的 ``[Include] user_config``，缺省用默认名。

    ``migoto_path`` 为空/不存在 → 返回 None（调用方据此整体 no-op，**不得**拿空串拼路径）。
    返回的路径**可能尚不存在**（3DMigoto 从未保存过持久变量时该文件不存在）。
    """
    if not migoto_path:
        return None
    root = Path(fs.normal_path(migoto_path))
    if not fs.is_dir(root):
        return None

    declared = _read_declared_user_config(root / "d3dx.ini")
    if not declared:
        return root / DEFAULT_USER_CONFIG_NAME
    candidate = Path(declared)
    # 上游判定：带盘符或以分隔符开头视为绝对路径，否则拼 3DMigoto 模块目录。
    if candidate.is_absolute() or declared.startswith(("\\", "/")):
        return candidate
    return root / candidate


def _read_declared_user_config(d3dx_ini: Path) -> str:
    """读取已配置路径，仅明确不存在时才使用默认文件名。"""
    try:
        text, _enc = decode_ini_bytes(fs.read_bytes(d3dx_ini))
    except FileNotFoundError:
        return ""
    section = ""
    for line in text.splitlines():
        m = _SECTION_RE.match(line)
        if m:
            section = m.group("name").strip().casefold()
            continue
        if section != "include":
            continue
        m = _USER_CONFIG_RE.match(line)
        if m:
            return m.group("value").strip().strip('"')
    return ""


# 编解码


def decode_ini_bytes(raw: bytes) -> tuple[str, str]:
    """按 BOM、严格 UTF-8、系统 ANSI 编码的顺序解码，返回 (text, encoding)。

    3DMigoto 分支和外部编辑器可能使用不同编码。有 BOM 时优先遵循标记；
    无 BOM 时先尝试 UTF-8，失败后使用 mbcs 和 surrogateescape 保留原始字节。

    纯 ASCII 文件在两种编码下字节相同，可按 UTF-8 无损读写。
    """
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig"), "utf-8-sig"
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        # 保留字节序和 BOM，包括大端编码文件。
        enc = "utf-16-le" if raw.startswith(b"\xff\xfe") else "utf-16-be"
        return raw.decode(enc), enc
    try:
        return raw.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        return raw.decode("mbcs", errors="surrogateescape"), "mbcs"


def encode_ini_text(text: str, encoding: str) -> bytes:
    """使用读取时确定的编码重新编码文本，保持用户 INI 的字节格式。"""
    if encoding == "mbcs":
        return text.encode("mbcs", errors="surrogateescape")
    if encoding in ("utf-8", "utf-8-sig"):
        # 直通字节可能带 surrogateescape 残留（override 层是按字节透传存的）
        return text.encode(encoding, errors="surrogateescape")
    return text.encode(encoding)


def decode_passthrough_text(text: str) -> str:
    """将字节透传产生的孤立代理码位还原为 Unicode 文本。

    容器 INI 覆盖文件使用 UTF-8 和 surrogateescape 保留作者的原始字节，
    因此 ANSI 编码的命名空间可能包含代理码位。此处恢复字节并按 decode_ini_bytes
    重新解码，使其能与用户 INI 中的 Unicode 命名空间匹配；无代理码位时原样返回。
    """
    if not text or not _SURROGATE_RE.search(text):
        return text
    try:
        raw = text.encode("utf-8", errors="surrogateescape")
    except UnicodeEncodeError:
        return text
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    try:
        return raw.decode("mbcs")
    except (UnicodeDecodeError, LookupError):
        return text


# 规范化与比较


def normalize_var_key(key: str) -> str:
    """统一键的编码、空白、路径分隔符和大小写。

    通过 decode_passthrough_text 还原透传字节，避免用户 INI 与 Mod INI
    使用不同解码结果而无法匹配。
    """
    return decode_passthrough_text(key.strip()).replace("/", "\\").casefold()


def key_variants(key: str) -> tuple[str, ...]:
    """返回键在不同编码下的等价解释，规范形式优先。

    部分字节序列同时符合 GBK 与 UTF-8，例如 D3 AB 在两种编码下对应不同字符。
    仅靠编码探测无法消除歧义，因此分别尝试以 UTF-8 和系统编码重新解释原始字节。
    ASCII 键在两种编码下相同，可直接返回。
    """
    base = normalize_var_key(key)
    out = [base]
    if base.isascii():
        return tuple(out)
    for src, dst in (("utf-8", "mbcs"), ("mbcs", "utf-8")):
        try:
            alt = normalize_var_key(base.encode(src, errors="surrogateescape").decode(dst))
        except (UnicodeError, LookupError):
            continue
        if alt and alt not in out:
            out.append(alt)
    return tuple(out)


def variant_pool(keys) -> frozenset[str]:
    """把一组键展开成「全部等价解释」的集合，供逐行 O(1) 判定是否命中。"""
    pool: set[str] = set()
    for key in keys:
        pool.update(key_variants(key))
    return frozenset(pool)


def key_in_pool(key: str, pool) -> bool:
    """该键在**任一等价解释**下是否落在 :func:`variant_pool` 里。"""
    return any(v in pool for v in key_variants(key))


def values_equal(left: str, right: str) -> bool:
    """按数值或文本规则比较两个变量值。

    双方均可解析为浮点数时比较数值，兼容上游 %.9g 输出中的等价表示；
    其他情况使用不区分大小写的文本比较。
    """
    a, b = left.strip(), right.strip()
    try:
        fa, fb = float(a), float(b)
    except ValueError:
        return a.casefold() == b.casefold()
    if fa == fb:
        return True
    # %.9g 只保留 9 位有效数字，容差按相对精度给，避免 1e-12 级抖动被当成用户改动
    scale = max(abs(fa), abs(fb), 1.0)
    return abs(fa - fb) <= scale * 1e-9


# 解析


def parse_persisted_values(text: str) -> dict[str, str]:
    """读取 Constants 段中的持久变量键值。

    同一规范键出现不同值时移除该键并报告诊断；重复的相同值可保留。
    """
    values: dict[str, str] = {}
    conflicted: set[str] = set()
    for _line_no, key, value, _m in _iter_persist_lines(text):
        if key in conflicted:
            continue
        prev = values.get(key)
        if prev is not None and not values_equal(prev, value):
            values.pop(key, None)
            conflicted.add(key)
            logger.warning("d3dx_user.ini 同键冲突值，已丢弃该键: %s", key)
            continue
        values[key] = value
    return values


def duplicate_persisted_keys(text: str) -> tuple[str, ...]:
    """重复键不能作为新的采集证据，即使值相同。"""
    seen, duplicates = set(), set()
    for _, key, _, _ in _iter_persist_lines(text):
        if key in seen:
            duplicates.add(key)
        seen.add(key)
    return tuple(sorted(duplicates))


def _iter_persist_lines(text: str):
    """产出 ``[Constants]`` 段内每个持久变量行：``(行号, 规范化键, 原始值, match)``。"""
    section = ""
    for line_no, line in enumerate(text.splitlines()):
        m = _SECTION_RE.match(line)
        if m:
            section = m.group("name").strip().casefold()
            continue
        if section != CONSTANTS_SECTION.casefold():
            continue
        m = _PERSIST_LINE_RE.match(line)
        if not m:
            continue
        key = m.group("key")
        if not key.lstrip().startswith("$"):
            continue
        yield line_no, normalize_var_key(key), m.group("value").strip(), m


# 稳定读取


def read_stable(
    path: str | Path,
    *,
    settle_seconds: float = 0.12,
    attempts: int = 5,
    sleep=time.sleep,
) -> UserIniSnapshot | None:
    """等待文件大小和修改时间稳定后读取并解析。

    文件不存在时返回 None，保留已保存偏好；观察时间内持续变化时抛出 UserIniUnstableError，
    避免以尚未写完的内容更新状态。
    """
    p = Path(fs.normal_path(path))
    try:
        initial = p.stat()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise UserIniUnstableError(f"{p}: {error}") from error
    if not stat.S_ISREG(initial.st_mode):
        raise UserIniUnstableError(tr("用户配置不是普通文件：{p}", p=p))

    last: tuple[int, int, int] | None = None
    for _ in range(max(1, attempts)):
        try:
            st = p.stat()
        except FileNotFoundError:
            return None
        except OSError as error:
            raise UserIniUnstableError(f"{p}: {error}") from error
        fingerprint = (st.st_size, st.st_mtime_ns, st.st_ino)
        if last == fingerprint:
            break
        last = fingerprint
        sleep(settle_seconds)
    else:
        raise UserIniUnstableError(str(p))

    try:
        raw = fs.read_bytes(p)
        after = p.stat()
        if (after.st_size, after.st_mtime_ns, after.st_ino) != last or len(raw) != after.st_size:
            raise UserIniUnstableError(tr("{p}: 文件在读取期间发生变化", p=p))
    except OSError as e:
        raise UserIniUnstableError(f"{p}: {e}") from e
    text, encoding = decode_ini_bytes(raw)
    return UserIniSnapshot(
        path=p,
        text=text,
        sha256=hashlib.sha256(raw).hexdigest(),
        encoding=encoding,
        values=parse_persisted_values(text),
    )


# 局部改写


def apply_values(text: str, updates: dict[str, str]) -> str:
    """把 ``updates``（规范化键 -> 值）写入文本，返回新文本。

    - 已存在的键**只替换值**，保留原有缩进、行内注释和换行风格。
    - 缺失的键追加到 ``[Constants]`` 段末尾（上游只会写这一个段）；文本里没有该段时
      在末尾补一个。
    """
    if not updates:
        return text
    pending = {normalize_var_key(k): v for k, v in updates.items()}
    aliases = {}
    for key in pending:
        for alias in key_variants(key):
            if alias in aliases and aliases[alias] != key:
                raise ValueError(tr("目标持久变量存在编码别名冲突"))
            aliases[alias] = key
    seen = set()
    newline = _detect_newline(text)
    lines = text.splitlines(keepends=True)
    entries = tuple(_iter_persist_lines(text))
    canonical_lines = {}
    for line_no, key, _value, _match in entries:
        if key in pending:
            canonical_lines.setdefault(key, line_no)

    for line_no, key, value, m in entries:
        # 与 remove_keys 使用相同的编码等价匹配，避免重复写入同一个逻辑键。
        matches = {aliases[v] for v in key_variants(key) if v in aliases}
        if len(matches) > 1:
            raise ValueError(tr("目标持久变量存在编码别名冲突"))
        matched = next(iter(matches), None)
        if matched is None:
            continue
        preferred = canonical_lines.get(matched, line_no)
        if matched in seen or preferred != line_no:
            lines[line_no] = ""
            continue
        seen.add(matched)
        new_value = pending[matched]
        if key == matched and values_equal(value, new_value):
            continue
        ending = lines[line_no][len(lines[line_no].rstrip("\r\n")) :]
        spelling = m.group("key").strip() if key == matched else matched
        lines[line_no] = f"{m.group('indent')}{spelling} = {new_value}{m.group('trailer')}{ending}"

    pending = {key: value for key, value in pending.items() if key not in seen}
    result = "".join(lines)
    if pending:
        # 按原始物理行插入，保留无关内容中的混合换行。
        physical = result.splitlines(keepends=True)
        start, end = -1, len(physical)
        for number, line in enumerate(physical):
            section = _SECTION_RE.match(line.lstrip("\ufeff") if number == 0 else line)
            if section:
                if start < 0 and section.group("name").strip().casefold() == "constants":
                    start = number
                elif start >= 0:
                    end = number
                    break
        addition = "".join(f"{key} = {value}{newline}" for key, value in pending.items())
        if start < 0:
            if result and not result.endswith(("\r", "\n")):
                result += newline
            result += f"[Constants]{newline}" + addition
        else:
            if end and not physical[end - 1].endswith(("\r", "\n")):
                physical[end - 1] += newline
            physical.insert(end, addition)
            result = "".join(physical)
    return result


def remove_keys(text: str, keys) -> str:
    """移除 [Constants] 中的目标偏好行，使加载器执行原有初始化逻辑。

    不推导或改写声明初值、Constants 命令及 post 命令。
    待删键和文件中的键可能来自不同编码解释，需通过 key_variants 等价匹配，
    防止残留偏好覆盖初始值。
    """
    pool = variant_pool(keys)
    if not pool:
        return text
    doomed = {line_no for line_no, key, _v, _m in _iter_persist_lines(text) if key_in_pool(key, pool)}
    if not doomed:
        return text
    return "".join(ln for i, ln in enumerate(text.splitlines(keepends=True)) if i not in doomed)


def sha256_of(text: str, encoding: str) -> str:
    """按实际写入字节计算哈希，与 UserIniSnapshot.sha256 一致，供写后校验。"""
    return hashlib.sha256(encode_ini_text(text, encoding)).hexdigest()


# 内部


def _detect_newline(text: str) -> str:
    """优先识别文本中的 CRLF 换行，否则选用 LF 作为写回换行符。"""
    return "\r\n" if "\r\n" in text else "\n"
