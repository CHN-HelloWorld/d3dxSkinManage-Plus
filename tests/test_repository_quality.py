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
