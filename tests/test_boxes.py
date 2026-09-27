"""盒结构层测试：长度越界拒绝、分片/加密布局拒绝、largesize 与 size==0。"""

import struct

import pytest

from mp4timeline.errors import (
    BoxLengthError,
    MissingBoxError,
    UnsupportedLayoutError,
)
from mp4timeline.mp4parse.boxes import iter_boxes
from mp4timeline.mp4parse.parser import parse_movie


def _box(typ: bytes, payload: bytes) -> bytes:
    return struct.pack(">I4s", 8 + len(payload), typ) + payload


class TestIterBoxes:
    def test_box_size_beyond_file_rejected(self, testlog):
        data = _box(b"ftyp", b"isom\0\0\0\0") + struct.pack(">I4s", 10_000, b"moov")
        with pytest.raises(BoxLengthError) as exc:
            list(iter_boxes(data, 0, len(data)))
        testlog.write(
            {
                "event": "assertion",
                "test": "box_size_beyond_file",
                "basis": "盒声明长度 10000 超出 36 字节文件，必须抛 BoxLengthError",
                "actual": str(exc.value),
            }
        )

    def test_box_size_smaller_than_header_rejected(self):
        data = struct.pack(">I4s", 4, b"free")  # size < 8
        with pytest.raises(BoxLengthError):
            list(iter_boxes(data, 0, len(data)))

    def test_trailing_bytes_rejected(self):
        data = _box(b"free", b"1234") + b"\0\0"  # 尾部不足 8 字节盒头
        with pytest.raises(BoxLengthError):
            list(iter_boxes(data, 0, len(data)))

    def test_largesize_box(self):
        payload = b"x" * 24
        data = struct.pack(">I4sQ", 1, b"free", 16 + len(payload)) + payload
        boxes = list(iter_boxes(data, 0, len(data)))
        assert len(boxes) == 1
        assert boxes[0].header_size == 16
        assert boxes[0].size == 40

    def test_size_zero_extends_to_parent_end(self):
        inner = _box(b"free", b"abcd")
        data = _box(b"ftyp", b"isom\0\0\0\0") + struct.pack(">I4s", 0, b"mdat") + inner
        boxes = list(iter_boxes(data, 0, len(data)))
        assert boxes[-1].type == "mdat"
        assert boxes[-1].payload_end == len(data)

    def test_child_beyond_parent_rejected(self):
        # 父盒 moov 声明 16 字节，但子盒声明越出父盒边界
        child = struct.pack(">I4s", 100, b"mvhd")
        parent = struct.pack(">I4s", 8 + len(child), b"moov") + child
        with pytest.raises(BoxLengthError):
            list(iter_boxes(parent, 8, len(parent)))


class TestLayoutRejection:
    def test_fragmented_rejected(self, generated_dir, testlog):
        with pytest.raises(UnsupportedLayoutError) as exc:
            parse_movie((generated_dir / "fragmented.mp4").read_bytes(), "fragmented.mp4")
        testlog.write(
            {
                "event": "assertion",
                "test": "fragmented_rejected",
                "basis": "顶层出现 moof 必须按 UnsupportedLayoutError 拒绝",
                "actual": f"{type(exc.value).__name__}: {exc.value}",
            }
        )

    def test_encrypted_rejected(self, generated_dir):
        with pytest.raises(UnsupportedLayoutError, match="encv"):
            parse_movie((generated_dir / "encrypted.mp4").read_bytes(), "encrypted.mp4")

    def test_bad_length_rejected(self, generated_dir):
        with pytest.raises(BoxLengthError):
            parse_movie((generated_dir / "bad_length.mp4").read_bytes(), "bad_length.mp4")

    def test_missing_moov_rejected(self):
        data = _box(b"ftyp", b"isom\0\0\0\0") + _box(b"free", b"xxxx")
        with pytest.raises(MissingBoxError):
            parse_movie(data, "<synthetic>")

    def test_mvex_inside_moov_rejected(self):
        mvhd = struct.pack(">I4s", 8 + 4 + 96, b"mvhd") + b"\0" * 4 + b"\0" * 96
        mvex = _box(b"mvex", b"\0" * 8)
        moov = _box(b"moov", mvhd + mvex)
        data = _box(b"ftyp", b"isom\0\0\0\0") + moov
        with pytest.raises(UnsupportedLayoutError, match="mvex"):
            parse_movie(data, "<synthetic>")
