"""验证源码说明、文本编码、文档链接和维护文件范围。"""

from __future__ import annotations

import pytest

from scripts.quality import check_repository


@pytest.mark.parametrize(
    "annotation",
    ["# ?????", '"""读取 ??? 数据。"""', "# 读取\ufffd数据。", "# noqa: E501 读取 ??? 数据。"],
)
def test_rejects_damaged_annotation_encoding(annotation):
    """编码损坏产生的替代字符和连续问号必须使注释检查失败。"""
    assert "注释包含疑似编码损坏的字符" in check_repository.annotation_errors(annotation)


@pytest.mark.parametrize("source", ['"""Cache"""\n', '#!/usr/bin/env python\n"""Cache"""\n'])
def test_rejects_module_documentation_without_chinese(tmp_path, monkeypatch, source):
    """仅有英文标识符的模块说明也必须报告位置，不能绕过说明语言检查。"""
    monkeypatch.setattr(check_repository, "ROOT", tmp_path)
    path = tmp_path / "src/example.py"
    path.parent.mkdir()
    path.write_text(source, encoding="utf-8", newline="\n")

    errors = check_repository.check_source(path)

    line = 2 if source.startswith("#!") else 1
    assert any(error.startswith(f"src/example.py:{line}:") and "模块文档字符串" in error for error in errors)


def test_reports_undocumented_private_and_nested_definitions(tmp_path, monkeypatch):
    """源码说明检查覆盖模块、私有类、构造方法和嵌套异步回调。"""
    monkeypatch.setattr(check_repository, "ROOT", tmp_path)
    path = tmp_path / "src/example.py"
    path.parent.mkdir()
    path.write_text(
        "class _Worker:\n"
        "    def __init__(self):\n"
        "        async def complete():\n"
        "            return None\n"
        "        self.complete = complete\n",
        encoding="utf-8",
        newline="\n",
    )

    errors = check_repository.check_source(path)

    missing = [error for error in errors if "缺少中文文档字符串" in error]
    assert len(missing) == 4
    assert any("_Worker.__init__.complete" in error for error in missing)
    assert all(error.startswith("src/example.py:") for error in missing)


def test_accepts_documented_protocol_and_property_accessors(tmp_path, monkeypatch):
    """协议空实现与属性读写方法可分别提供职责说明。"""
    monkeypatch.setattr(check_repository, "ROOT", tmp_path)
    path = tmp_path / "src/example.py"
    path.parent.mkdir()
    path.write_text(
        '"""演示状态访问协议。"""\n'
        "class State:\n"
        '    """提供状态的读写契约。"""\n'
        "    @property\n"
        "    def value(self):\n"
        '        """读取当前状态。"""\n'
        "        ...\n"
        "    @value.setter\n"
        "    def value(self, value):\n"
        '        """替换当前状态。"""\n'
        "        ...\n",
        encoding="utf-8",
        newline="\n",
    )

    assert check_repository.check_source(path) == []


def test_keeps_test_names_outside_documentation_gate(tmp_path, monkeypatch):
    """行为明确的测试名称不强制添加重复的文档字符串。"""
    monkeypatch.setattr(check_repository, "ROOT", tmp_path)
    path = tmp_path / "tests/test_sample.py"
    path.parent.mkdir()
    path.write_text("def test_empty_selection_is_rejected():\n    pass\n", encoding="utf-8", newline="\n")
    assert check_repository.check_source(path) == []


def test_invalid_document_encoding_is_reported_without_aborting_link_checks(tmp_path, monkeypatch):
    monkeypatch.setattr(check_repository, "ROOT", tmp_path)
    document = tmp_path / "docs/invalid.md"
    document.parent.mkdir()
    document.write_bytes(b"\xff\xfe")
    (tmp_path / "README.md").write_text("[缺失页面](missing.md)\n", encoding="utf-8")

    errors = check_repository.check_document_links()

    assert any("docs/invalid.md" in error and "UTF-8" in error for error in errors)
    assert any("README.md" in error and "missing.md" in error for error in errors)


def test_scans_maintained_files_without_user_data_or_build_output(tmp_path, monkeypatch):
    monkeypatch.setattr(check_repository, "ROOT", tmp_path)
    maintained = (
        "README.md",
        "src/module.py",
        "tests/fixtures/sample.json",
        "docs/guide.md",
        "config/languages/zh-CN/strings.json",
        "resources/icon.svg",
        ".github/workflows/quality.yml",
        "plugins/example.mod-inventory/entry.py",
        "scripts/launch/open_experience.py",
    )
    excluded = (
        "data/sample.json",
        "config/games.json",
        "build/package/README.md",
        "updater/bin/generated.json",
        "updater/obj/generated.json",
        "src/__pycache__/module.pyc",
        "plugins/custom/entry.py",
        "local-library/sample.json",
    )
    for relative in (*maintained, *excluded):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}\n", encoding="utf-8")

    found = {path.relative_to(tmp_path).as_posix() for path in check_repository.public_files()}

    assert set(maintained) <= found
    assert not set(excluded) & found


def test_does_not_scan_generated_source_files(tmp_path, monkeypatch):
    monkeypatch.setattr(check_repository, "ROOT", tmp_path)
    maintained = tmp_path / "src/module.py"
    generated = tmp_path / "src/build/module.py"
    maintained.parent.mkdir(parents=True)
    generated.parent.mkdir(parents=True)
    maintained.write_text("value = 1\n", encoding="utf-8")
    generated.write_text("value = 2\n", encoding="utf-8")

    found = check_repository.source_files()

    assert maintained in found
    assert generated not in found


@pytest.mark.parametrize(
    "annotation",
    [
        "# 按当前游戏筛选，防止混入其他游戏的下载记录。",
        "# Qt 工作线程退出后再释放对象。",
        "# 提交阶段保留原文件，失败时恢复。",
        "# 按照外部 API 的字段约定读取下载量。",
    ],
)
def test_allows_production_explanations(annotation):
    assert check_repository.annotation_errors(annotation) == []


@pytest.mark.parametrize(
    "relative",
    ["docs/sample.md", "sample.JSON", "config/languages/zh-CN/strings.json", "resources/licenses/component.txt"],
)
def test_checks_text_encoding(tmp_path, monkeypatch, relative):
    """所有维护文本都需要有效的 UTF-8 编码。"""
    monkeypatch.setattr(check_repository, "ROOT", tmp_path)
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xff\xfe")

    assert any("UTF-8" in error for error in check_repository.check_text_encoding(path))


def test_binary_resources_do_not_require_utf8(tmp_path, monkeypatch):
    """二进制图片不参与文本编码检查。"""
    monkeypatch.setattr(check_repository, "ROOT", tmp_path)
    path = tmp_path / "image.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\n")

    assert check_repository.check_text_encoding(path) == []


def test_accepts_utf8_public_text(tmp_path, monkeypatch):
    """有效文本可以包含完整的中文和技术标识。"""
    monkeypatch.setattr(check_repository, "ROOT", tmp_path)
    path = tmp_path / "README.md"
    path.write_text("# 使用说明\n\n通过 API 导入样例条目。\n", encoding="utf-8")

    assert check_repository.check_text_encoding(path) == []
