"""Encoding unit tests: RLE/bit-packed hybrid, PLAIN values, thrift roundtrip."""
import struct

import pytest

from app.format.encodings import (
    bit_width_for, decode_levels, encode_levels, frame_levels,
)
from app.format.plain import decode_values, encode_values
from app.format.thrift import decode_struct, encode_struct
from app.format.parquet_thrift import FileMetaData, PageHeader
from app.kernel.schema import PhysicalType


# --------------------------------------------------------------------------- #
# RLE / bit-packed hybrid
# --------------------------------------------------------------------------- #

def test_bit_width():
    assert bit_width_for(0) == 0
    assert bit_width_for(1) == 1
    assert bit_width_for(2) == 2
    assert bit_width_for(5) == 3
    assert bit_width_for(7) == 3
    assert bit_width_for(8) == 4


@pytest.mark.parametrize("seq,width", [
    ([0, 0, 0, 0, 0, 0, 0, 0, 0, 0], 1),       # pure long RLE run of zeros
    ([1, 1, 1, 1, 0, 0, 0, 0, 1, 1], 2),
    ([0, 2, 0, 2, 5, 1, 0, 2, 3, 4], 3),
    ([7, 7, 7, 7, 7, 7, 7, 7, 7, 7], 3),
    ([0, 1, 2, 3, 4, 5, 6, 7], 3),
    ([0], 1),
    ([5, 3, 1, 0, 2, 4, 7, 6, 5, 5, 5, 5, 5, 5, 5, 5, 5], 3),
])
def test_hybrid_roundtrip(seq, width):
    blob = encode_levels(seq, width)
    got = decode_levels(blob, width, len(seq)).levels
    assert got == seq


def test_rle_header_uses_even_low_bit():
    # RLE run of eight zeros width 3 is header (8<<1)=0x10 then value 0x00.
    blob = encode_levels([0] * 8, 3)
    assert blob == bytes([0x10, 0x00])


def test_bitpacked_irregular_values_decode():
    # Directly decode a hand-built bit-packed run (the writer prefers RLE, but
    # readers -- including PyArrow files -- must understand bit-packing).
    # header 0x03 = one group (8 values), low bit set; values 0..7 width 3.
    packed = bytes([0x03]) + bytes.fromhex("88c6fa")
    got = decode_levels(packed, 3, 8, 0).levels
    assert got == [0, 1, 2, 3, 4, 5, 6, 7]


def test_bitpacked_final_group_is_padded():
    # Two real values in an 8-value group padded with zeros.
    packed = bytes([0x03, 0x01, 0x00, 0x00])  # width1: values [1,0,...]
    got = decode_levels(packed, 1, 2, 0).levels
    assert got == [1, 0]


def test_frame_has_four_byte_length_prefix():
    frame = frame_levels([0, 1, 2, 0, 1], 2)
    length = struct.unpack_from("<I", frame, 0)[0]
    assert length == len(frame) - 4


# --------------------------------------------------------------------------- #
# PLAIN values
# --------------------------------------------------------------------------- #

def test_plain_int32_negative_is_twos_complement():
    blob = encode_values([-1, 2147483647], PhysicalType.INT32)
    assert blob[:4] == struct.pack("<i", -1)
    values, _ = decode_values(blob, PhysicalType.INT32, 2)
    assert values == [-1, 2147483647]


def test_plain_string_length_prefixed_utf8():
    blob = encode_values(["héllo", ""], PhysicalType.BYTE_ARRAY)
    values, consumed = decode_values(blob, PhysicalType.BYTE_ARRAY, 2)
    assert values == ["héllo", ""]
    assert consumed == len(blob)


def test_plain_boolean_lsb_packing():
    blob = encode_values([True, False, True, True], PhysicalType.BOOLEAN)
    # bits LSB-first: 0b1101 = 0x0D
    assert blob == bytes([0x0D])
    values, _ = decode_values(blob, PhysicalType.BOOLEAN, 4)
    assert values == [True, False, True, True]


def test_plain_double_roundtrip():
    vals = [0.0, -1.5, 3.25, 1e100]
    blob = encode_values(vals, PhysicalType.DOUBLE)
    got, _ = decode_values(blob, PhysicalType.DOUBLE, len(vals))
    assert got == vals


# --------------------------------------------------------------------------- #
# Thrift compact roundtrip
# --------------------------------------------------------------------------- #

def test_thrift_footer_roundtrip():
    md = {
        "version": 1,
        "schema": [{"name": b"schema", "num_children": 1}],
        "num_rows": 3,
        "row_groups": [],
        "created_by": "test/1",
    }
    blob = encode_struct(FileMetaData, md)
    back, pos = decode_struct(FileMetaData, blob)
    assert pos == len(blob)
    assert back["version"] == 1
    assert back["num_rows"] == 3
    assert back["created_by"] == b"test/1"
    assert back["schema"][0]["name"] == b"schema"


def test_thrift_page_header_roundtrip():
    ph = {
        "type": 0,
        "uncompressed_page_size": 17,
        "compressed_page_size": 17,
        "data_page_header": {
            "num_values": 5, "encoding": 0,
            "definition_level_encoding": 3,
            "repetition_level_encoding": 3},
    }
    blob = encode_struct(PageHeader, ph)
    back, _ = decode_struct(PageHeader, blob)
    assert back["type"] == 0
    assert back["data_page_header"]["num_values"] == 5
