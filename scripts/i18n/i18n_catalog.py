"""提取、创建和验证可编辑语言包，无需启动 Qt。

--update 更新原文目录；--new 创建语言包；--check 检查目录和翻译完整性。
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.utils.language_pack import PACK_FILENAME, normalize_language_code, read_language_pack  # noqa: E402


def extract_messages(source: str, *, extra_names=()) -> list[str]:
    """通过语法树识别翻译接口及其别名，按首次出现顺序去重提取静态字符串原文。"""
    tree = ast.parse(source)
    names = {
        item.asname or item.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module
        in {
            "src.utils.i18n",
            "src.utils.language_pack",
        }
        for item in node.names
        if item.name in {"t", "N_"}
    }
    names.update(extra_names)
    messages: dict[str, None] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name) or node.func.id not in names:
            continue
        if not node.args or not isinstance(node.args[0], ast.Constant) or not isinstance(node.args[0].value, str):
            continue
        messages[node.args[0].value] = None
    return list(messages)


def extract_cpp_messages(source: str) -> list[str]:
    """提取原生源码中 Tr/N_ 调用的字面量，跳过 C++ 注释。"""
    token_pattern = r'//[^\n]*|/\*[\s\S]*?\*/|L?"(?:\\.|[^"\\])*"|[A-Za-z_]\w*|[^\s]'
    tokens = [
        match.group() for match in re.finditer(token_pattern, source) if not match.group().startswith(("//", "/*"))
    ]
    result: dict[str, None] = {}
    for index, token in enumerate(tokens[:-2]):
        if token not in {"Tr", "N_"} or tokens[index + 1] != "(":
            continue
        parts = []
        cursor = index + 2
        while cursor < len(tokens) and tokens[cursor].startswith(('L"', '"')):
            literal = tokens[cursor]
            if literal.startswith('L"'):
                literal = literal[1:]
            try:
                parts.append(ast.literal_eval(literal))
            except (SyntaxError, ValueError):
                parts = []
                break
            cursor += 1
        if parts and cursor < len(tokens) and tokens[cursor] in {",", ")"}:
            result["".join(parts)] = None
    return list(result)


def collect_catalog(root: Path = ROOT) -> list[str]:
    """按稳定文件顺序合并 Python 和原生更新器源码中的翻译原文，并保持唯一条目。"""
    result: dict[str, None] = {}
    for path in sorted((root / "src").rglob("*.py")):
        messages = extract_messages(path.read_text(encoding="utf-8-sig"))
        result.update(dict.fromkeys(messages))
    for path in sorted((root / "Updater" / "src").rglob("*")):
        if path.suffix in {".cpp", ".h"}:
            result.update(dict.fromkeys(extract_cpp_messages(path.read_text(encoding="utf-8-sig"))))
    return list(result)


def create_pack(root: Path, code: str, name: str, messages, categories, qt=()) -> Path:
    """校验语言代码并独占创建包含消息、分类及 Qt 文案的语言包，目标存在时保留已有翻译。"""
    normalized = normalize_language_code(code)
    if not normalized:
        raise ValueError("Use a language folder code such as fr-FR, ja-JP or zh-Hant")
    path = root / normalized / PACK_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "$schema": "../schema.json",
        "_meta": {"format": 1, "name": name or normalized},
        "messages": dict.fromkeys(messages, ""),
        "gamebanana": dict.fromkeys(categories, ""),
        "qt": dict.fromkeys(qt, ""),
    }
    # 以独占方式创建文件，防止重复执行时覆盖已有翻译。
    with path.open("x", encoding="utf-8") as output:
        json.dump(data, output, ensure_ascii=False, indent=2)
        output.write("\n")
    return path


def main(argv=None) -> int:
    """按参数更新原文模板或创建语言包，校验已发现语言包及指定完整语言，违规时返回非零状态。"""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--update", action="store_true", help="Update the Chinese template from source messages")
    parser.add_argument("--new", metavar="LOCALE", help="Create a new pack without overwriting an existing file")
    parser.add_argument("--name", default="", help="Native language name shown in Settings")
    parser.add_argument("--check", action="store_true", help="Validate JSON and placeholders in all packs")
    parser.add_argument("--require-complete", action="append", default=[], metavar="LOCALE")
    args = parser.parse_args(argv)
    root = ROOT / "config" / "languages"
    catalog = collect_catalog()
    base_path = root / "zh-CN" / PACK_FILENAME
    base = json.loads(base_path.read_text(encoding="utf-8-sig"))
    if args.update:
        previous = base.get("messages", {})
        # 以当前原文作为键，保留用户已编辑的译文。
        base["messages"] = {source: previous.get(source) or source for source in catalog}
        base_path.write_text(json.dumps(base, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"Updated {len(catalog)} source messages in the Chinese template.")
    if args.new:
        try:
            path = create_pack(root, args.new, args.name, catalog, base.get("gamebanana", {}), base.get("qt", {}))
        except (OSError, ValueError) as error:
            print(f"Cannot create language pack: {error}")
            return 1
        print(path)
    failures = []
    found = set()
    required = {normalize_language_code(code) for code in args.require_complete}
    for folder in sorted(root.iterdir()):
        code = normalize_language_code(folder.name)
        path = folder / PACK_FILENAME
        if not code or not folder.is_dir() or not path.is_file():
            continue
        found.add(code)
        try:
            pack = read_language_pack(path)
        except (OSError, ValueError, UnicodeError, RecursionError) as error:
            failures.append(f"{code}: {error}")
            continue
        failures.extend(f"{code}: {error}" for error in pack.diagnostics)
        missing = [source for source in catalog if source not in pack.messages]
        if code in required:
            untranslated = [
                source
                for source in catalog
                if code != "zh-CN"
                and pack.messages.get(source) == source
                and any("\u3400" <= character <= "\u9fff" for character in source)
            ]
            if missing or untranslated:
                failures.append(f"{code}: {len(missing)} missing and {len(untranslated)} untranslated messages")
        print(
            f"{code}: {len(catalog) - len(missing)}/{len(catalog)} messages; "
            f"{len(pack.gamebanana)} categories; {len(pack.qt)} Qt strings"
        )
    for code in required - found:
        failures.append(f"Required language {code!r} is missing")
    if failures:
        print("\n".join(failures))
        return 1
    print("Language packs are valid. Partial packs fall back to the source text.")
    return 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    raise SystemExit(main())
