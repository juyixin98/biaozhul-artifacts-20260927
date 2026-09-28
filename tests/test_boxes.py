"""Box-layer tests: out-of-bounds lengths, unsupported layouts, truncated
tables — each failure must raise the exact classified error."""

import logging
import struct

import pytest

from fixtures import expected
from mp4timeline.boxes import iter_boxes, parse_movie
from mp4timeline.errors import (
    BoxOutOfBoundsError,
    MalformedBoxError,
    MissingBoxError,
    MP4Error,
    UnsupportedEncryptionError,
    UnsupportedFeatureError,
    UnsupportedLayoutError,
)
from mp4timeline.timeline import build_movie_timeline

from .conftest import fixture_path

log = logging.getLogger("mp4timeline.tests")

ERROR_TYPES = {
    "box_out_of_bounds": BoxOutOfBoundsError,
    "unsupported_fragmented_layout": UnsupportedLayoutError,
    "unsupported_encryption": UnsupportedEncryptionError,
    "malformed_box": MalformedBoxError,
    "unsupported_feature": UnsupportedFeatureError,
}


@pytest.mark.parametrize("name", sorted(expected.BAD_FIXTURES))
def test_bad_fixture_rejected_with_category(name, run_identity):
    category = expected.BAD_FIXTURES[name]
    with open(fixture_path(name), "rb") as fh:
        data = fh.read()
    log.info("RUN=%s CASE=%s expect_category=%s", run_identity, name, category)
    with pytest.raises(ERROR_TYPES[category]) as excinfo:
        build_movie_timeline(parse_movie(data))
    log.info("RUN=%s CASE=%s got category=%s detail=%s",
             run_identity, name, excinfo.value.category, excinfo.value)
    assert excinfo.value.category == category


def test_box_size_one_largesize_accepted():
    # a well-formed free box using 64-bit largesize must parse
    payload = b"\x00" * 4
    box = struct.pack(">I4sQ", 1, b"free", 16 + len(payload)) + payload
    boxes = list(iter_boxes(box, 0, len(box)))
    assert len(boxes) == 1 and boxes[0].size == 20 and boxes[0].header == 16


def test_box_size_zero_extends_to_parent_end():
    box = struct.pack(">I4s", 0, b"free") + b"\x00" * 6
    boxes = list(iter_boxes(box, 0, len(box)))
    assert boxes[0].size == len(box)


def test_box_size_smaller_than_header_rejected():
    with pytest.raises(MalformedBoxError):
        list(iter_boxes(struct.pack(">I4s", 4, b"free"), 0, 8))


def test_trailing_garbage_rejected():
    with pytest.raises(MalformedBoxError):
        list(iter_boxes(b"\x00\x00\x00\x08free" + b"\x00\x00", 0, 10))


def test_missing_moov_rejected():
    with pytest.raises(MissingBoxError):
        parse_movie(struct.pack(">I4s", 8, b"free"))


def test_error_categories_are_stable_strings():
    # the API and job store persist these strings; they must not drift
    assert MP4Error.category == "mp4_error"
    for cat in expected.BAD_FIXTURES.values():
        assert cat in ERROR_TYPES, f"uncategorized expected failure {cat}"
