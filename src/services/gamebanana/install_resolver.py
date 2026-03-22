"""根据 GameBanana 分类信息建议本地安装目标。

只有接口分类名与本地目录英文名或原名精确匹配时才允许自动安装。
标题、文件名和其他模糊匹配仅用于候选排序，其分值低于自动安装阈值。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from rapidfuzz import fuzz

from src.models.node import Node

#: 词切分：只保留字母数字与 CJK，其余（空格/下划线/连字符/括号…）都当分隔符。
_TOKEN_RE = re.compile("[0-9a-z一-鿿]+")

# 将不同字形的撇号和重音符统一为 ASCII 撇号，消除接口与用户输入的格式差异。
_APOSTROPHES = "’‘ʼ`´′"

# 模糊匹配仅用于排序，分数低于 1；只有分类标签精确匹配才允许自动安装。
FUZZY_CEILING = 0.99


def _norm(s: str) -> str:
    """规范化名称以比较大小写、变音符号、撇号和分隔符差异。

    例如 Node d'Azur、node d’azur 与 Node dAzur 规范化后相等，仍视为精确匹配。
    """
    if not s:
        return ""
    for ch in _APOSTROPHES:
        s = s.replace(ch, "'")
    # NFKD 拆出组合用变音符号后丢掉它们：é→e、ü→u。
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return "".join(_TOKEN_RE.findall(s.casefold()))


def _strip_archive_ext(file_name: str) -> str:
    """去除一层已知归档扩展名，并将常见文件名分隔符转换为空格供名称匹配。"""
    fn = file_name or ""
    for ext in (".zip", ".7z", ".rar", ".tar", ".gz"):
        if fn.lower().endswith(ext):
            fn = fn[: -len(ext)]
            break
    return fn.replace("_", " ").replace("-", " ").replace(".", " ")


def _is_distinctive(name: str) -> bool:
    """判断名称长度是否足以使用子串和模糊匹配。

    过短的拉丁名称容易命中无关标题，仅允许整词匹配；
    包含两个及以上 CJK 字符的名称可使用更宽松的匹配。
    """
    has_cjk = any("一" <= ch <= "鿿" for ch in name)
    return len(name) >= (2 if has_cjk else 3)


@dataclass
class ScoredCandidate:
    """保存候选分类的标识、名称、层级路径和匹配分数，供安装目标选择界面使用。"""

    node_id: str
    display_name: str
    score: float
    #: 目录当前的英文名，供安装弹窗判断是否可回填在线分类名称。
    name_en: str = ""
    #: 从顶层分类到候选目录的显示名称，供用户区分不同层级中的同名目录。
    category_path: str = ""


def _category_path(node: Node, nodes_by_id: dict[str, Node]) -> str:
    """沿父节点链构造候选分类的显示路径，父节点缺失或出现环时停止。"""
    names = []
    seen = set()
    while node is not None and node.id not in seen:
        seen.add(node.id)
        names.append(node.display_name)
        node = nodes_by_id.get(node.parent_id)
    return " / ".join(reversed(names))


def _score_one(haystack: str, name: str, name_en: str) -> float:
    """计算候选排序分数，范围为 0～1；不参与自动安装决策。"""
    candidates = [c for c in (name, name_en) if c]
    if not candidates:
        return 0.0
    best = 0.0
    h = (haystack or "").lower()
    tokens: set[str] | None = None
    for c in candidates:
        cl = c.lower().strip()
        if not cl:
            continue
        if not _is_distinctive(cl):
            # 短名称仅允许整词匹配，例如 A 可匹配 Mod A Pack，不能匹配单词 Package 内的字符。
            if tokens is None:
                tokens = set(_TOKEN_RE.findall(h))
            if cl in tokens:
                best = max(best, 1.0)
            continue
        if cl in h:
            return 1.0
        # 候选名中的有效词项完整出现在文件名中时，作为较强的匹配依据。
        # 适用于多词英文名仅保留姓名中具有区分度的部分，
        # 例如 Node_summer.zip 可匹配 Project Node。

        toks = [t for t in cl.split() if len(t) >= 3]
        if toks and any(t in h for t in toks):
            best = max(best, 0.92)
            continue
        ratio = max(
            fuzz.partial_ratio(cl, h) / 100.0,
            fuzz.token_set_ratio(cl, h) / 100.0,
        )
        best = max(best, ratio)
    return best


class InstallTargetResolver:
    """根据分类及文件信息匹配本地安装目录。"""

    def match_by_category(
        self,
        category_name: str,
        candidates: list[Node],
    ) -> list[Node]:
        """按分类标签精确匹配本地目录，返回全部候选。

        优先比较目录英文名；未填写英文名时使用目录原名。
        """
        cat = _norm(category_name)
        if not cat:
            return []
        hits = [c for c in candidates if c.name_en and _norm(c.name_en) == cat]
        if hits:
            return hits
        return [c for c in candidates if c.name and _norm(c.name) == cat]

    def resolve(
        self,
        mod_name: str,
        file_name: str,
        candidates: list[Node],
        *,
        category_name: str = "",
        category_is_sub: bool = False,
    ) -> tuple[Node | None, list[ScoredCandidate]]:
        """返回可自动安装的唯一目录及排序后的候选列表。

        category_is_sub 表示分类属于父分区。值为 False 时分类名称可能只是根分区名，
        不能据此自动选择角色目录；缺省值为 False。
        """
        exact_ids: set[str] = set()
        if category_is_sub:
            exact_ids = {c.id for c in self.match_by_category(category_name, candidates)}

        title_basis = mod_name or ""
        fn = _strip_archive_ext(file_name)

        ranked: list[tuple[Node, float]] = []
        for ch in candidates:
            if ch.id in exact_ids:
                ranked.append((ch, 1.0))
                continue
            s1 = _score_one(title_basis, ch.name, ch.name_en)
            s2 = _score_one(fn, ch.name, ch.name_en)
            ranked.append((ch, min(max(s1, s2 * 0.85), FUZZY_CEILING)))

        ranked.sort(key=lambda kv: kv[1], reverse=True)
        nodes_by_id = {node.id: node for node in candidates}
        scored = [
            ScoredCandidate(
                node_id=ch.id,
                display_name=ch.display_name,
                score=s,
                name_en=ch.name_en or "",
                category_path=_category_path(ch, nodes_by_id),
            )
            for ch, s in ranked
        ]
        # 分类标签仅精确命中一个目标时自动安装；无匹配或存在同名目标时由用户选择。
        if len(exact_ids) != 1:
            return None, scored
        best_id = next(iter(exact_ids))
        return next(c for c in candidates if c.id == best_id), scored
