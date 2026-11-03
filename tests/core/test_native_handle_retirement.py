"""验证原生目录身份索引在删除和移动后退役空分支。"""

import json
import os

import pytest

from src.core.watcher_windows import NativeContainerHandles

pytestmark = pytest.mark.skipif(os.name != "nt", reason="需要 Windows 原生文件身份接口")


def _track(handles, path, *, category=False):
    """创建真实标记并确认测试文件系统支持按身份跟踪。"""
    path.mkdir(parents=True, exist_ok=True)
    marker = path / (".d3dxcategory" if category else ".d3dxmod")
    marker.write_text(json.dumps({"id" if category else "mod_id": path.name}), encoding="utf-8")
    before = len(handles._handles)
    handles.track(path)
    if len(handles._handles) == before:
        pytest.skip("测试目录不支持按文件编号打开")
    return marker


def test_deleted_containers_retire_empty_path_branches(tmp_path):
    handles = NativeContainerHandles()
    try:
        for index in range(12):
            path = tmp_path / str(index)
            marker = _track(handles, path)
            marker.unlink()
            path.rmdir()
            assert handles.inspect(path)[0] == "delete_pending"
            assert not handles._handles
            assert not handles._paths
    finally:
        handles.close()


def test_retiring_child_preserves_live_category_identity(tmp_path):
    handles = NativeContainerHandles()
    parent = tmp_path / "category"
    try:
        _track(handles, parent, category=True)
        child = parent / "mod"
        marker = _track(handles, child)
        marker.unlink()
        child.rmdir()
        assert handles.inspect(child)[0] == "delete_pending"
        assert len(handles._handles) == 1
        assert handles._node(child) is None
        assert handles._node(parent)["record"][1] == "category"
    finally:
        handles.close()


def test_relocating_only_container_retires_old_empty_ancestors(tmp_path):
    handles = NativeContainerHandles()
    source = tmp_path / "old" / "mod"
    destination = tmp_path / "new" / "mod"
    try:
        _track(handles, source)
        destination.parent.mkdir()
        assert source.resolve().is_relative_to(tmp_path.resolve())
        assert destination.resolve().is_relative_to(tmp_path.resolve())
        source.rename(destination)
        assert handles.inspect(source)[0] == "relocated"
        assert handles._node(source.parent) is None
        assert handles.containing_mod(destination / "body.ini") == str(destination)
        assert len(handles._handles) == 1
    finally:
        handles.close()
