"""检查源码说明、中文文档覆盖、文本编码、文档链接及原生代码格式。"""

from __future__ import annotations

import ast
import io
import re
import shutil
import subprocess
import sys
import tokenize
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[2]
SOURCE_SUFFIXES = {".py", ".cpp", ".h", ".qss", ".sh", ".bat"}
PUBLIC_ROOTS = (
    "src",
    "tests",
    "scripts",
    "docs",
    "resources",
    "updater",
    ".github",
    "config/languages",
    "plugins",
)
GENERATED_DIRS = {"__pycache__", ".pytest_cache", ".ruff_cache", ".vs", "bin", "obj", "build", "dist"}
TEXT_SUFFIXES = SOURCE_SUFFIXES | {
    ".md",
    ".rst",
    ".txt",
    ".json",
    ".jsonc",
    ".toml",
    ".yml",
    ".yaml",
    ".xml",
    ".svg",
    ".html",
    ".css",
    ".js",
    ".ini",
    ".cfg",
    ".rc",
    ".vcxproj",
    ".filters",
    ".props",
    ".sln",
    ".manifest",
    ".jsonl",
    ".csv",
    ".ps1",
    ".psm1",
    ".psd1",
    ".cmd",
    ".conf",
    ".ui",
    ".qrc",
}
HAN = re.compile(r"[\u3400-\u9fff]")
ENGLISH_PROSE = re.compile(r"[A-Za-z]{3,}[\s-]+[A-Za-z]{2,}")
DIRECTIVE = re.compile(r"^\s*(?:#!|#\s*(?:noqa|type|pyright|mypy|ruff|fmt|pragma|coding)\s*[:=])")
NAMESPACE = re.compile(r"^//\s*namespace(?:\s+[\w:]+)?\s*$")
MARKDOWN_LINK = re.compile(r"(?<!!)\[[^\]\n]+\]\(([^)\n]+)\)")


def source_files() -> list[Path]:
    """源码与公开文件共用维护范围，不包含用户数据或生成目录。"""
    return [path for path in public_files() if path.suffix.lower() in SOURCE_SUFFIXES]


def public_files() -> list[Path]:
    """枚举受维护的公开文件，排除用户数据和生成目录。"""
    files = {path for path in ROOT.iterdir() if path.is_file()}
    for relative in PUBLIC_ROOTS:
        files.update(
            path
            for path in (ROOT / relative).rglob("*")
            if path.is_file() and not GENERATED_DIRS.intersection(path.relative_to(ROOT).parts)
        )
    return sorted(files)


def check_text_encoding(path: Path) -> list[str]:
    """检查维护文本的 UTF-8 编码，二进制资源不参与文本解码。"""
    relative = path.relative_to(ROOT).as_posix()
    if path.suffix.lower() not in TEXT_SUFFIXES and not path.name.startswith(".") and path.name != "LICENSE":
        return []
    try:
        path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        return [f"{relative}: 无法按 UTF-8 读取：{error}"]
    return []


def python_annotations(source: str):
    """通过语法解析提取注释，避免把字符串中的井号视为注释。"""
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.COMMENT:
            yield token.start[0], token.string
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            value = ast.get_docstring(node, clean=False)
            if value:
                yield node.body[0].lineno, value


def c_annotations(source: str):
    """跳过普通字符串和 C++ 原始字符串后提取注释。"""
    offset = 0
    while offset < len(source):
        if source.startswith('R"', offset):
            opening = source.find("(", offset + 2, offset + 19)
            if opening >= 0:
                closing = ")" + source[offset + 2 : opening] + '"'
                end = source.find(closing, opening + 1)
                offset = len(source) if end < 0 else end + len(closing)
                continue
        if source[offset] in "\"'":
            quote = source[offset]
            offset += 1
            while offset < len(source):
                if source[offset] == "\\":
                    offset += 2
                elif source[offset] == quote:
                    offset += 1
                    break
                else:
                    offset += 1
            continue
        if source.startswith("//", offset) or source.startswith("/*", offset):
            start = offset
            if source.startswith("//", offset):
                end = source.find("\n", offset)
                offset = len(source) if end < 0 else end
            else:
                end = source.find("*/", offset + 2)
                offset = len(source) if end < 0 else end + 2
            yield source.count("\n", 0, start) + 1, source[start:offset]
            continue
        offset += 1


def annotation_errors(value: str) -> list[str]:
    """检查说明语言、正式用语和文本编码损坏，保留工具指令及命名空间标记。"""
    errors = []
    if re.search(r"\?{3,}|\ufffd", value):
        errors.append("注释包含疑似编码损坏的字符")
    if DIRECTIVE.match(value) or NAMESPACE.fullmatch(value):
        return errors
    if not HAN.search(value) and ENGLISH_PROSE.search(value):
        errors.append("说明性注释应使用中文")
    return errors


def documentation_errors(source: str, relative: str) -> list[str]:
    """检查维护源码中的全部文档作用域；测试不强制逐函数说明。"""
    if relative.startswith("tests/"):
        return []
    tree = ast.parse(source)
    errors = []
    module_docstring = ast.get_docstring(tree)
    if not module_docstring:
        errors.append(f"{relative}:1: 模块缺少中文文档字符串")
    elif not HAN.search(module_docstring):
        errors.append(f"{relative}:{tree.body[0].lineno}: 模块文档字符串应包含中文说明")

    def visit(node: ast.AST, prefix: str = "") -> None:
        """递归检查命名定义，保留类和嵌套函数的限定名称以便定位。"""
        for child in ast.iter_child_nodes(node):
            name = prefix
            if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                name = f"{prefix}.{child.name}" if prefix else child.name
                docstring = ast.get_docstring(child)
                if not docstring:
                    errors.append(f"{relative}:{child.lineno}: {name} 缺少中文文档字符串")
                elif not HAN.search(docstring):
                    errors.append(f"{relative}:{child.body[0].lineno}: {name} 文档字符串应包含中文说明")
            visit(child, name)

    visit(tree)
    return errors


def check_source(path: Path) -> list[str]:
    """校验源码编码、换行、注释内容及 Python 定义的中文文档覆盖。"""
    relative = path.relative_to(ROOT).as_posix()
    errors = []
    try:
        data = path.read_bytes()
        source = data.decode("utf-8")
    except (OSError, UnicodeError) as error:
        return [f"{relative}: 无法按 UTF-8 读取：{error}"]
    if data.startswith(b"\xef\xbb\xbf"):
        errors.append(f"{relative}: 源码应使用无 BOM 的 UTF-8")
        source = source.lstrip("\ufeff")
    if path.suffix == ".bat":
        if b"\n" in data.replace(b"\r\n", b""):
            errors.append(f"{relative}: Windows 批处理应使用 CRLF")
    elif b"\r" in data:
        errors.append(f"{relative}: 源码应使用 LF")
    if data and not data.endswith(b"\n"):
        errors.append(f"{relative}: 文件末尾应保留换行")
    try:
        if path.suffix == ".py":
            errors.extend(documentation_errors(source, relative))
            annotations = python_annotations(source)
        elif path.suffix in {".cpp", ".h", ".qss"}:
            annotations = c_annotations(source)
        else:
            prefix = re.compile(r"\s*(?:#|rem\b)", re.IGNORECASE)
            annotations = ((number, line) for number, line in enumerate(source.splitlines(), 1) if prefix.match(line))
        for line, value in annotations:
            errors.extend(f"{relative}:{line}: {message}" for message in annotation_errors(value))
    except (SyntaxError, tokenize.TokenError) as error:
        errors.append(f"{relative}: 语法解析失败：{error}")
    return errors


def check_document_links() -> list[str]:
    """检查公开 Markdown 文档中的本地链接目标，跳过外部地址和页内锚点。"""
    files = [path for path in public_files() if path.suffix.lower() == ".md"]
    errors = []
    for path in files:
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            errors.append(f"{path.relative_to(ROOT).as_posix()}: 无法按 UTF-8 读取：{error}")
            continue
        for match in MARKDOWN_LINK.finditer(source):
            target = match.group(1).strip("<>")
            if urlsplit(target).scheme or target.startswith("#"):
                continue
            target = unquote(target.split("#", 1)[0])
            if target and not (path.parent / target).exists():
                errors.append(f"{path.relative_to(ROOT).as_posix()}: 文档链接不存在：{target}")
    return errors


def main() -> int:
    """汇总公开文件、源码、文档链接及原生格式检查，发现违规时返回非零状态。"""
    files = source_files()
    published = public_files()
    errors = [error for path in files for error in check_source(path)]
    errors.extend(error for path in published for error in check_text_encoding(path))
    errors.extend(check_document_links())
    executable = Path(sys.executable).parent / ("clang-format.exe" if sys.platform == "win32" else "clang-format")
    formatter = str(executable) if executable.is_file() else shutil.which("clang-format")
    if formatter is None:
        errors.append("未找到 clang-format，请安装项目开发依赖。")
    else:
        native_files = [str(path) for path in files if path.suffix in {".cpp", ".h"}]
        result = subprocess.run([formatter, "--dry-run", "--Werror", *native_files], capture_output=True, text=True)
        if result.returncode:
            errors.append("C++ 格式检查失败，请使用 clang-format 18.1.8 整理：\n" + result.stderr)
    if errors:
        print("\n".join(errors))
        print(f"仓库规范检查发现 {len(errors)} 项问题。")
        return 1
    print(f"仓库规范检查通过：{len(files)} 个源码文件、{len(published)} 个公开文件，文档链接与 C++ 格式有效。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
