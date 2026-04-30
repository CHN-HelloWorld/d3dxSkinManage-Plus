from __future__ import annotations

import hashlib
import json
import os
import struct
import time
import zlib

import pytest
from PIL import Image

from src.core.category_preset import (
    CategoryPresetCancelled,
    CategoryPresetDocument,
    CategoryPresetError,
    CategoryPresetNode,
)
from src.services.category_preset_file import (
    copy_category_preset_image,
    read_category_preset,
    validate_category_image,
    write_category_preset,
)


def raw_file(path, manifest=None, images=b"", raw_manifest=None):
    data = manifest or {
        "kind": "d3dxSkinManage-Plus.category-preset",
        "nodes": [
            {"path": "角色", "node_type": "category"},
        ],
    }
    encoded = raw_manifest if raw_manifest is not None else json.dumps(data, ensure_ascii=False).encode("utf-8")
    payload = encoded + images
    path.write_bytes(
        struct.pack("<8sIQ32s", b"D3DXCP\r\n", len(encoded), len(images), hashlib.sha256(payload).digest()) + payload
    )
    return path


def make_image(path, fmt="PNG"):
    Image.new("RGB", (7, 5), (20, 60, 200)).save(path, format=fmt)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_file_with_distinct_creation_and_modification_times_is_readable(tmp_path):
    target = raw_file(tmp_path / "timestamps.d3dxcp")
    # 跨过文件系统的时间戳精度，使创建时间与后续变更时间不同。
    time.sleep(0.02)
    os.utime(target, ns=(1_000_000_000, 1_000_000_000))

    loaded = read_category_preset(target)

    assert loaded.nodes[0].path == "角色"
    assert loaded.source_path == target.resolve()


def test_real_binary_roundtrip_and_header_contract(tmp_path):
    target = tmp_path / "测试.d3dxcp"
    document = CategoryPresetDocument("示例", (CategoryPresetNode("角色", "角色", "category"),))
    progress = []
    write_category_preset(target, document, {}, progress=lambda phase, percent: progress.append((phase, percent)))
    raw = target.read_bytes()
    magic, manifest_length, image_length, digest = struct.unpack("<8sIQ32s", raw[:52])
    assert magic == b"D3DXCP\r\n"
    assert image_length == 0
    assert len(raw) == 52 + manifest_length
    assert digest == hashlib.sha256(raw[52:]).digest()
    manifest = json.loads(raw[52:])
    assert manifest["kind"] == "d3dxSkinManage-Plus.category-preset"
    assert not {"version", "format_version", "schema_version"} & manifest.keys()
    loaded = read_category_preset(target)
    assert loaded.nodes[0].path == "角色"
    assert loaded.source_path == target.resolve()
    assert loaded.payload_sha256 == digest.hex()
    assert progress and progress[-1][1] == 100
    assert all(
        isinstance(phase, str) and isinstance(percent, int) and 0 <= percent <= 100 for phase, percent in progress
    )


@pytest.mark.parametrize(
    "fmt,media_type",
    [
        ("PNG", "image/png"),
        ("JPEG", "image/jpeg"),
        ("BMP", "image/bmp"),
        ("GIF", "image/gif"),
        ("WEBP", "image/webp"),
    ],
)
def test_supported_images_preserve_original_bytes_and_shared_ids(tmp_path, fmt, media_type):
    image_path = tmp_path / "source.bin"
    image_id = make_image(image_path, fmt)
    assert validate_category_image(image_path) == (image_id, media_type, image_path.stat().st_size)
    document = CategoryPresetDocument(
        "带图",
        (
            CategoryPresetNode("角色", "角色", "category", image_id=image_id),
            CategoryPresetNode("角色/示例角色丁", "示例角色丁", "character", image_id=image_id),
        ),
    )
    target = tmp_path / "带图.d3dxcp"
    write_category_preset(target, document, {image_id: image_path})
    loaded = read_category_preset(target)
    assert len(loaded.images) == 1
    assert loaded.images[0].media_type == media_type
    extracted = tmp_path / "caller-chosen.bin"
    copy_category_preset_image(loaded, image_id, extracted)
    assert extracted.read_bytes() == image_path.read_bytes()


def test_reader_accepts_defaults_unknown_fields_and_uppercase_suffix(tmp_path):
    target = raw_file(
        tmp_path / "默认.D3DXCP",
        {
            "kind": "d3dxSkinManage-Plus.category-preset",
            "schema_version": 1000,
            "nodes": [{"path": "角色", "node_type": "category", "destination": "C:/outside"}],
        },
    )
    loaded = read_category_preset(target)
    assert loaded.name == "默认"
    assert loaded.nodes[0].name == "角色"
    assert loaded.nodes[0].sort_order == 0


@pytest.mark.parametrize(
    "raw_manifest",
    [
        b'{"kind":"d3dxSkinManage-Plus.category-preset","kind":"d3dxSkinManage-Plus.category-preset","nodes":[]}',
        b'{"unknown":{"x":1,"x":2},"kind":"d3dxSkinManage-Plus.category-preset","nodes":[{"path":"a","node_type":"category"}]}',
        b'{"kind":"d3dxSkinManage-Plus.category-preset","nodes":[{"path":"a","node_type":"category"}],"x":NaN}',
        b'{"kind":"d3dxSkinManage-Plus.category-preset","nodes":[{"path":"a","node_type":"category"}],"x":1e999}',
        b"\xff",
        b"[]",
        b"{",
    ],
)
def test_bad_json_is_rejected_even_with_correct_payload_digest(tmp_path, raw_manifest):
    target = raw_file(tmp_path / "bad.d3dxcp", raw_manifest=raw_manifest)
    with pytest.raises(CategoryPresetError):
        read_category_preset(target)


@pytest.mark.parametrize(
    "mutation", ["magic", "digest", "truncated", "trailer", "zero_manifest", "huge_manifest", "huge_images"]
)
def test_invalid_header_lengths_and_payload_are_rejected(tmp_path, mutation):
    target = raw_file(tmp_path / "bad.d3dxcp")
    content = bytearray(target.read_bytes())
    if mutation == "magic":
        content[0] = 0
    elif mutation == "digest":
        content[20] ^= 1
    elif mutation == "truncated":
        del content[-1]
    elif mutation == "trailer":
        content += b"extra"
    elif mutation == "zero_manifest":
        content[8:12] = struct.pack("<I", 0)
    elif mutation == "huge_manifest":
        content[8:12] = struct.pack("<I", 16 * 1024 * 1024 + 1)
    else:
        content[12:20] = struct.pack("<Q", 2**64 - 1)
    target.write_bytes(content)
    with pytest.raises(CategoryPresetError):
        read_category_preset(target)


@pytest.mark.parametrize("mode", ["corrupt", "wrong_digest", "wrong_type", "unindexed"])
def test_invalid_image_content_rejects_whole_file(tmp_path, mode):
    image_path = tmp_path / "source.png"
    image_id = make_image(image_path)
    image_bytes = image_path.read_bytes()
    if mode == "corrupt":
        image_bytes = b"not an image"
        image_id = hashlib.sha256(image_bytes).hexdigest()
    elif mode == "wrong_digest":
        image_id = "a" * 64
    data = {
        "kind": "d3dxSkinManage-Plus.category-preset",
        "nodes": [
            {"path": "a", "node_type": "category", "image_id": image_id},
        ],
        "images": [
            {
                "id": image_id,
                "offset": 0,
                "length": len(image_bytes),
                "media_type": "image/jpeg" if mode == "wrong_type" else "image/png",
            }
        ],
    }
    if mode == "unindexed":
        data["images"] = []
        del data["nodes"][0]["image_id"]
    target = raw_file(tmp_path / "bad.d3dxcp", data, image_bytes)
    with pytest.raises(CategoryPresetError):
        read_category_preset(target)


def test_wrong_input_type_and_extension_are_rejected(tmp_path):
    for target in (tmp_path, raw_file(tmp_path / "pretend.zip"), tmp_path / "missing.d3dxcp"):
        with pytest.raises(CategoryPresetError):
            read_category_preset(target)


def test_writer_rejects_unreferenced_missing_and_mismatched_images(tmp_path):
    source = tmp_path / "a.png"
    image_id = make_image(source)
    target = tmp_path / "output.d3dxcp"
    plain = CategoryPresetDocument("a", (CategoryPresetNode("a", "a", "category"),))
    pictured = CategoryPresetDocument("a", (CategoryPresetNode("a", "a", "category", image_id=image_id),))
    for document, mapping in (
        (plain, {image_id: source}),
        (pictured, {}),
        (pictured, {image_id: tmp_path / "missing"}),
    ):
        with pytest.raises(CategoryPresetError):
            write_category_preset(target, document, mapping)
        assert not target.exists()
    source.write_bytes(b"changed")
    with pytest.raises(CategoryPresetError):
        write_category_preset(target, pictured, {image_id: source})
    assert not target.exists()


def test_cancellation_stops_read_write_and_copy_without_partial_output(tmp_path):
    source = tmp_path / "source.png"
    image_id = make_image(source)
    document = CategoryPresetDocument("a", (CategoryPresetNode("a", "a", "category", image_id=image_id),))
    target = tmp_path / "cancel.d3dxcp"
    with pytest.raises(CategoryPresetCancelled):
        write_category_preset(target, document, {image_id: source}, cancelled=lambda: True)
    assert not target.exists()
    write_category_preset(target, document, {image_id: source})
    with pytest.raises(CategoryPresetCancelled):
        read_category_preset(target, cancelled=lambda: True)
    loaded = read_category_preset(target)
    copied = tmp_path / "cancel.png"
    with pytest.raises(CategoryPresetCancelled):
        copy_category_preset_image(loaded, image_id, copied, cancelled=lambda: True)
    assert not copied.exists()


def test_copy_rejects_changed_source_and_unknown_image_without_writing(tmp_path):
    source = tmp_path / "source.png"
    image_id = make_image(source)
    document = CategoryPresetDocument("a", (CategoryPresetNode("a", "a", "category", image_id=image_id),))
    target = tmp_path / "a.d3dxcp"
    write_category_preset(target, document, {image_id: source})
    loaded = read_category_preset(target)
    destination = tmp_path / "copy.png"
    with pytest.raises(CategoryPresetError):
        copy_category_preset_image(loaded, "../outside", destination)
    assert not destination.exists()
    content = bytearray(target.read_bytes())
    content[-1] ^= 1
    target.write_bytes(content)
    with pytest.raises(CategoryPresetError):
        copy_category_preset_image(loaded, image_id, destination)
    assert not destination.exists()


def test_writer_does_not_overwrite_existing_file_on_failure(tmp_path):
    target = tmp_path / "existing.d3dxcp"
    target.write_bytes(b"keep existing")
    document = CategoryPresetDocument("a", (CategoryPresetNode("a", "a", "category"),))
    with pytest.raises((CategoryPresetError, FileExistsError)):
        write_category_preset(target, document, {})
    assert target.read_bytes() == b"keep existing"


def test_pixel_limit_is_checked_before_attempting_pixel_allocation(tmp_path):
    target = tmp_path / "huge.png"
    ihdr = struct.pack(">IIBBBBB", 40_000_001, 1, 8, 2, 0, 0, 0)

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    target.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", b"") + chunk(b"IEND", b""))
    with pytest.raises(CategoryPresetError, match="40,000,000"):
        validate_category_image(target)


def test_sparse_file_limits_are_rejected_without_loading_the_file(tmp_path):
    target = tmp_path / "too-large.d3dxcp"
    with target.open("wb") as output:
        output.seek(1024 * 1024 * 1024)
        output.write(b"x")
    with pytest.raises(CategoryPresetError, match="1 GiB"):
        read_category_preset(target)
    target = tmp_path / "too-large.png"
    with target.open("wb") as output:
        output.seek(20 * 1024 * 1024)
        output.write(b"x")
    with pytest.raises(CategoryPresetError, match="20 MiB"):
        validate_category_image(target)


def test_json_depth_limit_applies_before_parsing_unknown_fields(tmp_path):
    prefix = b'{"kind":"d3dxSkinManage-Plus.category-preset","nodes":[{"path":"a","node_type":"category"}],"extra":'
    target = raw_file(tmp_path / "deep.d3dxcp", raw_manifest=prefix + b"[" * 10000 + b"0" + b"]" * 10000 + b"}")
    with pytest.raises(CategoryPresetError, match="16"):
        read_category_preset(target)


def test_cancel_during_payload_hash_stops_after_a_chunk(tmp_path):
    data = {
        "kind": "d3dxSkinManage-Plus.category-preset",
        "nodes": [{"path": "a", "node_type": "category"}],
        "padding": "x" * (2 * 1024 * 1024),
    }
    target = raw_file(tmp_path / "large-manifest.d3dxcp", data)
    cancel = False
    percentages = []

    def progress(phase, percent):
        nonlocal cancel
        percentages.append(percent)
        cancel = True

    with pytest.raises(CategoryPresetCancelled):
        read_category_preset(target, cancelled=lambda: cancel, progress=progress)
    assert percentages and percentages[-1] < 100


def test_cancel_during_image_write_removes_only_the_partial_output(tmp_path):
    source = tmp_path / "large.bmp"
    Image.new("RGB", (800, 800), (30, 60, 120)).save(source)
    image_id = hashlib.sha256(source.read_bytes()).hexdigest()
    document = CategoryPresetDocument("a", (CategoryPresetNode("a", "a", "category", image_id=image_id),))
    target = tmp_path / "cancel.d3dxcp"
    cancel = False

    def progress(phase, percent):
        nonlocal cancel
        if phase == "写入文件":
            cancel = True

    with pytest.raises(CategoryPresetCancelled):
        write_category_preset(target, document, {image_id: source}, cancelled=lambda: cancel, progress=progress)
    assert not target.exists()
    assert source.is_file()


def test_writer_performs_real_readback_and_cleans_up_changed_output(tmp_path):
    source = tmp_path / "source.png"
    image_id = make_image(source)
    document = CategoryPresetDocument("a", (CategoryPresetNode("a", "a", "category", image_id=image_id),))
    target = tmp_path / "readback.d3dxcp"

    def progress(phase, percent):
        if phase == "校验文件" and percent == 100:
            with target.open("r+b") as output:
                output.seek(-1, 2)
                byte = output.read(1)
                output.seek(-1, 2)
                output.write(bytes([byte[0] ^ 1]))

    with pytest.raises(CategoryPresetError):
        write_category_preset(target, document, {image_id: source}, progress=progress)
    assert not target.exists()


def test_truncated_pixel_data_and_unsupported_image_type_are_rejected(tmp_path):
    source = tmp_path / "source.bin"
    make_image(source, "JPEG")
    source.write_bytes(source.read_bytes()[:-15])
    with pytest.raises(CategoryPresetError):
        validate_category_image(source)
    make_image(source, "TIFF")
    with pytest.raises(CategoryPresetError):
        validate_category_image(source)


def two_frame_gif(path):
    Image.new("RGB", (2, 2), "red").save(
        path,
        format="GIF",
        save_all=True,
        append_images=[Image.new("RGB", (2, 2), "blue")],
        disposal=2,
        duration=100,
    )
    data = bytearray(path.read_bytes())
    # 定位样本中的实际帧描述符，排除偶然出现的逗号字节。
    descriptor = b",\x00\x00\x00\x00\x02\x00\x02\x00"
    first = data.index(descriptor)
    second = data.index(descriptor, first + len(descriptor))
    return data, first, second


@pytest.mark.parametrize("frame", ["first", "later", "offset", "canvas"])
@pytest.mark.parametrize("container", [False, True], ids=["image", "preset"])
def test_gif_frame_bounds_are_rejected_before_native_allocation(tmp_path, monkeypatch, frame, container):
    source = tmp_path / "oversize.gif"
    data, first, second = two_frame_gif(source)
    if frame == "canvas":
        data[6:10] = struct.pack("<HH", 7000, 7000)
    elif frame == "offset":
        data[second + 1 : second + 5] = struct.pack("<HH", 7000, 7000)
    else:
        index = first if frame == "first" else second
        data[index + 5 : index + 9] = struct.pack("<HH", 7000, 7000)
    source.write_bytes(data)
    if container:
        image_id = hashlib.sha256(data).hexdigest()
        source = raw_file(
            tmp_path / "oversize.d3dxcp",
            {
                "kind": "d3dxSkinManage-Plus.category-preset",
                "nodes": [{"path": "a", "node_type": "category", "image_id": image_id}],
                "images": [{"id": image_id, "offset": 0, "length": len(data), "media_type": "image/gif"}],
            },
            bytes(data),
        )

    # 保留真实 Pillow 解码，但在分配大尺寸原生缓冲前中止，以控制失败时的内存开销。
    oversized_attempts = []
    for name in ("fill", "new"):
        original = getattr(Image.core, name)

        def guarded(mode, size, *args, _original=original):
            if size[0] * size[1] > 40_000_000:
                oversized_attempts.append(size)
                raise AssertionError(f"Oversized native image allocation: {size}")
            return _original(mode, size, *args)

        monkeypatch.setattr(Image.core, name, guarded)
    operation = read_category_preset if container else validate_category_image
    with pytest.raises(CategoryPresetError):
        operation(source)
    assert oversized_attempts == []


@pytest.mark.parametrize("kept_descriptor_bytes", [1, 8, 9])
def test_corrupt_later_gif_descriptor_is_a_category_preset_error(tmp_path, kept_descriptor_bytes):
    source = tmp_path / "truncated.gif"
    data, _, second = two_frame_gif(source)
    source.write_bytes(data[: second + 1 + kept_descriptor_bytes])
    with pytest.raises(CategoryPresetError):
        validate_category_image(source)


def test_valid_animated_gif_preserves_all_original_frame_bytes(tmp_path):
    source = tmp_path / "valid.gif"
    data, _, _ = two_frame_gif(source)
    image_id = hashlib.sha256(data).hexdigest()
    assert validate_category_image(source) == (image_id, "image/gif", len(data))
    document = CategoryPresetDocument("a", (CategoryPresetNode("a", "a", "category", image_id=image_id),))
    target = tmp_path / "animated.d3dxcp"
    write_category_preset(target, document, {image_id: source})
    copied = tmp_path / "copy.gif"
    copy_category_preset_image(read_category_preset(target), image_id, copied)
    assert copied.read_bytes() == bytes(data)
