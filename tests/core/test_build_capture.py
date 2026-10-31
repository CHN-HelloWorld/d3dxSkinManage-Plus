import importlib.util
import sys
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image


def _load_release_capture(monkeypatch):
    """执行构建配置处理后的真实捕获库源码，并禁止加载 OpenCV。"""
    from scripts.release import build

    spec = importlib.util.find_spec("windows_capture")
    if spec is None:
        pytest.skip("需要 Windows 捕获依赖")
    source = Path(spec.origin).read_text(encoding="utf-8")
    command = build.build_command(Namespace(onefile=False, console=False, nuitka_args=[]))
    for option in command:
        if not option.startswith("--user-package-configuration-file="):
            continue
        pytest.importorskip("nuitka")
        from nuitka.plugins.standard.AntiBloatPlugin import NuitkaPluginAntiBloat
        from nuitka.utils.Yaml import getYamlPackage

        config = getYamlPackage().safe_load(Path(option.split("=", 1)[1]).read_text(encoding="utf-8"))
        plugin = object.__new__(NuitkaPluginAntiBloat)
        plugin.show_changes = False
        for entry in config:
            if entry["module-name"] == "windows_capture":
                for rule in entry.get("anti-bloat", ()):
                    source = plugin._onModuleSourceCode("windows_capture", "windows_capture", "", rule, source)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "cv2", None)
    monkeypatch.setitem(sys.modules, "windows_capture", module)
    exec(compile(source, spec.origin, "exec"), module.__dict__)
    return module


def test_release_capture_preserves_frame_pixels_without_opencv(monkeypatch):
    capture = _load_release_capture(monkeypatch)
    pixels = np.array([[[3, 20, 210, 128], [40, 50, 60, 255]]], dtype=np.uint8)
    frame = capture.Frame(pixels, 2, 1, 123)
    cropped = frame.crop(1, 0, 2, 1).convert_to_bgr()
    assert cropped.frame_buffer.tolist() == [[[40, 50, 60]]]
    assert (cropped.width, cropped.height, cropped.timespan) == (1, 1, 123)
    assert capture.WindowsCapture is not None
    assert capture.NativeWindowsCapture is not None


@pytest.mark.parametrize("channels", [3, 4])
def test_release_capture_saves_png_with_correct_colors_and_alpha(monkeypatch, tmp_path, channels):
    capture = _load_release_capture(monkeypatch)
    pixels = np.array([[[3, 20, 210, 128], [40, 50, 60, 255]]], dtype=np.uint8)
    frame = capture.Frame(pixels[..., :channels], 2, 1, 0)
    target = tmp_path / "捕获.png"
    frame.save_as_image(str(target))
    with Image.open(target) as image:
        assert image.size == (2, 1)
        assert image.getpixel((0, 0)) == ((210, 20, 3, 128) if channels == 4 else (210, 20, 3))


def test_release_capture_saves_bgra_as_jpeg(monkeypatch, tmp_path):
    capture = _load_release_capture(monkeypatch)
    pixels = np.full((8, 8, 4), [40, 80, 160, 100], dtype=np.uint8)
    target = tmp_path / "capture.JPG"
    capture.Frame(pixels, 8, 8, 0).save_as_image(str(target))
    with Image.open(target) as image:
        assert image.mode == "RGB"
        assert image.getpixel((0, 0)) == pytest.approx((160, 80, 40), abs=3)


@pytest.mark.parametrize("color_format", ["bgra8", "rgba8", "rgba16f"])
def test_release_dxgi_saves_converted_frame_without_opencv(monkeypatch, tmp_path, color_format):
    capture = _load_release_capture(monkeypatch)
    pixels = np.array([[[0, 0, 255, 255]]], dtype=np.uint8)
    if color_format == "rgba8":
        pixels = np.array([[[255, 0, 0, 255]]], dtype=np.uint8)
    elif color_format == "rgba16f":
        pixels = np.array([[[1, 0, 0, 1]]], dtype=np.float16)
    native = SimpleNamespace(
        width=1,
        height=1,
        color_format=color_format,
        bytes_per_pixel=pixels.nbytes,
        bytes_per_row=pixels.nbytes,
        buffer_view=lambda: memoryview(pixels),
    )
    frame = capture.DxgiDuplicationFrame(native)
    target = tmp_path / "dxgi.png"
    frame.save_as_image(str(target))
    with Image.open(target) as image:
        assert image.getpixel((0, 0)) == (255, 0, 0)
