"""在加载插件前对 Python 文件执行静态安全扫描。

禁止访问 src.plugin_sdk 之外的宿主内部模块；进程、网络和原生接口等
受限模块需要声明对应权限；动态执行调用始终拒绝。
其他标准库和第三方模块允许导入并记录日志。

静态扫描不提供操作系统级隔离，插件在宿主进程中运行。
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path

from src.utils.i18n import t as tr

# 允许直接 import 的唯一 src 子树
_ALLOWED_SRC_PREFIX = "src.plugin_sdk"

# 危险模块 → 所需权限（顶层模块名）
_DANGEROUS_MODULE_PERMS: dict[str, str] = {
    "subprocess": "process",
    "multiprocessing": "process",
    "socket": "network",
    "ssl": "network",
    "http": "network",
    "urllib": "network",
    "httpx": "network",
    "requests": "network",
    "aiohttp": "network",
    "ftplib": "network",
    "smtplib": "network",
    "ctypes": "native",
    "cffi": "native",
    "winreg": "native",
}

# 动态执行 RCE 原语（裸函数名）
_FORBIDDEN_CALLS = {"eval", "exec", "compile", "__import__"}
# 危险属性调用：模块名 -> {方法名}
_FORBIDDEN_ATTR_CALLS: dict[str, set[str]] = {
    "os": {"system", "popen", "execv", "execve", "execvp", "execvpe", "spawnv", "spawnl"},
}


@dataclass
class ScanResult:
    """汇总插件静态扫描中的违规导入、危险调用、权限检查和解析错误。"""

    forbidden_imports: list[str] = field(default_factory=list)  # 禁止的 src.* 内部 import
    dangerous_calls: list[str] = field(default_factory=list)  # 禁止动态执行和系统命令调用。
    ungranted: list[str] = field(default_factory=list)  # 危险模块缺权限
    gated_ok: list[str] = field(default_factory=list)  # 危险模块已授权（记录用）
    parse_errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """仅在未发现违规导入、危险调用、缺失权限或解析错误时返回 True。"""
        return not (self.forbidden_imports or self.dangerous_calls or self.ungranted or self.parse_errors)

    def summary(self) -> str:
        """将各类扫描违规去重并组合为可展示的本地化摘要。"""
        parts = []
        if self.forbidden_imports:
            parts.append(tr("禁止导入宿主内部: {details}", details=", ".join(sorted(set(self.forbidden_imports)))))
        if self.ungranted:
            parts.append(tr("危险模块缺权限: {details}", details=", ".join(sorted(set(self.ungranted)))))
        if self.dangerous_calls:
            parts.append(tr("动态执行/危险调用: {details}", details=", ".join(sorted(set(self.dangerous_calls)))))
        if self.parse_errors:
            parts.append(tr("解析失败: {details}", details="; ".join(self.parse_errors)))
        return " | ".join(parts)


def _top_module(name: str) -> str:
    """提取完整模块路径的首级模块名。"""
    return (name or "").split(".")[0]


def _is_forbidden_src(mod: str) -> bool:
    """判断导入是否触及插件 SDK 以外的宿主内部模块。"""
    if not mod:
        return False
    if mod == "src.plugin_sdk" or mod.startswith(_ALLOWED_SRC_PREFIX + "."):
        return False
    return mod == "src" or mod.startswith("src.")


class _Visitor(ast.NodeVisitor):
    """遍历插件语法树，按导入和直接调用形式收集静态扫描结果。"""

    def __init__(self, granted: frozenset[str], rel: str, result: ScanResult) -> None:
        """绑定已授予权限、文件相对路径和共享扫描结果。"""
        self._granted = granted
        self._rel = rel
        self._r = result

    def _check_module(self, mod: str, lineno: int) -> None:
        """检查宿主内部导入及危险模块权限，并附上文件位置。"""
        if not mod:
            return
        if _is_forbidden_src(mod):
            self._r.forbidden_imports.append(f"{mod} ({self._rel}:{lineno})")
            return
        # URL 解析只处理本地字符串；urllib 本身及其请求模块仍受网络权限检查。
        if mod == "urllib.parse":
            return
        perm = _DANGEROUS_MODULE_PERMS.get(_top_module(mod))
        if perm:
            tag = f"{mod}→{perm} ({self._rel}:{lineno})"
            if perm in self._granted:
                self._r.gated_ok.append(tag)
            else:
                self._r.ungranted.append(tag)

    def visit_Import(self, node: ast.Import) -> None:
        """逐项检查普通导入声明，再继续遍历子节点。"""
        for alias in node.names:
            self._check_module(alias.name, node.lineno)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        # 相对 import（level>0）是插件包内部，放行
        """检查绝对导入声明，插件包内相对导入由包自身管理。"""
        if node.level and not node.module:
            return
        if node.level == 0 and node.module:
            if node.module == "urllib":
                for alias in node.names:
                    self._check_module(f"urllib.{alias.name}", node.lineno)
            else:
                self._check_module(node.module, node.lineno)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        """记录明确匹配规则的动态执行和危险调用，再继续检查嵌套表达式。"""
        f = node.func
        if isinstance(f, ast.Name) and f.id in _FORBIDDEN_CALLS:
            self._r.dangerous_calls.append(f"{f.id}() ({self._rel}:{node.lineno})")
        elif isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
            bad = _FORBIDDEN_ATTR_CALLS.get(f.value.id)
            if bad and f.attr in bad:
                self._r.dangerous_calls.append(f"{f.value.id}.{f.attr}() ({self._rel}:{node.lineno})")
        self.generic_visit(node)


def scan_plugin(plugin_dir: Path, granted_permissions) -> ScanResult:
    """扫描插件目录下全部 .py，返回 ScanResult。granted_permissions: 已授权权限集合。"""
    granted = frozenset(granted_permissions or ())
    result = ScanResult()
    for py in sorted(plugin_dir.rglob("*.py")):
        rel = str(py.relative_to(plugin_dir))
        if py.is_symlink() or not py.resolve().is_relative_to(plugin_dir.resolve()):
            result.parse_errors.append(tr("源码不能是链接或越界路径：{path}", path=rel))
            continue
        try:
            tree = ast.parse(py.read_text("utf-8"), filename=rel)
        except (SyntaxError, OSError, ValueError) as e:
            result.parse_errors.append(f"{rel}: {e}")
            continue
        _Visitor(granted, rel, result).visit(tree)
    return result
