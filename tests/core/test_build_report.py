from xml.etree import ElementTree

import pytest


@pytest.mark.parametrize("encoding", ["utf8", "utf-8"])
def test_compilation_report_with_chinese_paths_parses_as_xml(tmp_path, encoding):
    from scripts.release import build

    report = tmp_path / "nuitka-report.xml"
    report.write_bytes(
        (f"<?xml version='1.0' encoding='{encoding}'?>\n<report source=\"项目/resources/图标.svg\" />").encode()
    )

    build.normalize_compilation_report(report)

    assert ElementTree.parse(report).getroot().get("source") == "项目/resources/图标.svg"
