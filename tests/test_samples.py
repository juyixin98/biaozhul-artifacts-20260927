"""样本表层测试：DTS/PTS/CTO、stsc 分组、字节范围 —— 对照手工时间表。"""

import hashlib
import json

import pytest

from mp4timeline.mp4parse.parser import parse_movie

# 手工推算的 bframes 参考表（与 fixtures/references/bframes.json 一致，
# 这里独立写一份字面量，避免测试只读同一个文件而失去交叉性）。
BFRAMES_EXPECTED = [
    # (index, dts, cto, pts, duration, size, is_sync)
    (0, 0,   0,    0,   100, 500, True),
    (1, 100, 200,  300, 100, 300, False),
    (2, 200, -100, 100, 100, 120, False),
    (3, 300, -100, 200, 100, 130, False),
    (4, 400, 100,  500, 100, 400, True),
    (5, 500, -100, 400, 100, 110, False),
]


@pytest.fixture(scope="module")
def bframes(generated_dir):
    data = (generated_dir / "bframes.mp4").read_bytes()
    return parse_movie(data, "bframes.mp4"), data


class TestBFrameReorder:
    def test_dts_cto_pts_table(self, bframes, testlog):
        movie, _ = bframes
        track = movie.tracks[0]
        actual = [
            (s.index, s.dts, s.cto, s.pts, s.duration, s.size, s.is_sync)
            for s in track.samples
        ]
        testlog.write(
            {
                "event": "assertion",
                "test": "bframes_dts_cto_pts",
                "basis": "手工时间表 BFRAMES_EXPECTED（ctts v1 有符号偏移）",
                "expected": BFRAMES_EXPECTED,
                "actual": actual,
            }
        )
        assert actual == BFRAMES_EXPECTED

    def test_pts_is_dts_plus_signed_cto(self, bframes):
        movie, _ = bframes
        for s in movie.tracks[0].samples:
            assert s.pts == s.dts + s.cto
        # 负偏移确实存在且符号保留
        ctos = [s.cto for s in movie.tracks[0].samples]
        assert min(ctos) == -100

    def test_decode_order_vs_presentation_order(self, bframes):
        movie, _ = bframes
        track = movie.tracks[0]
        decode_order = [s.index for s in track.samples]
        presentation_order = [
            s.index for s in sorted(track.samples, key=lambda s: s.pts)
        ]
        assert decode_order == [0, 1, 2, 3, 4, 5]
        assert presentation_order == [0, 2, 3, 1, 5, 4]

    def test_byte_ranges_recover_exact_payloads(self, bframes, generated_dir, testlog):
        """解析器给出的字节范围读出的内容必须等于构建器的确定性载荷。

        载荷 sha1 来自 fixtures/generated/payloads.json（构建器独立记录），
        不由被测核心生成。
        """

        movie, data = bframes
        payloads = json.loads((generated_dir / "payloads.json").read_text())
        track_payloads = payloads["bframes.mp4"]["1"]
        mismatches = []
        for s in movie.tracks[0].samples:
            digest = hashlib.sha1(data[s.byte_offset : s.byte_offset + s.size]).hexdigest()
            if digest != track_payloads[str(s.index)]:
                mismatches.append(s.index)
        testlog.write(
            {
                "event": "assertion",
                "test": "bframes_byte_ranges",
                "basis": "payloads.json（夹具构建器记录的样本 sha1）",
                "expected": "全部样本 sha1 一致",
                "actual": "一致" if not mismatches else f"不一致: {mismatches}",
            }
        )
        assert mismatches == []

    def test_byte_ranges_contiguous_per_chunk(self, bframes):
        """stsc=(1,2,1)：每 chunk 2 样本，chunk 内字节范围必须首尾相接。"""

        movie, _ = bframes
        samples = movie.tracks[0].samples
        for a, b in zip(samples, samples[1:]):
            if (a.index // 2) == (b.index // 2):  # 同 chunk
                assert b.byte_offset == a.byte_offset + a.size


class TestSampleTableValidation:
    def test_stts_count_mismatch_rejected(self):
        from mp4timeline.errors import SampleTableError
        from mp4timeline.mp4parse.sample_table import build_samples

        with pytest.raises(SampleTableError, match="stts"):
            build_samples(
                stts=[(3, 100)],  # 3 个样本
                ctts=None,
                stsc=[(1, 2, 1)],
                sizes=[10, 20],  # 但只有 2 个大小
                chunk_offsets=[0],
                sync_numbers=None,
            )

    def test_stsc_total_mismatch_rejected(self):
        from mp4timeline.errors import SampleTableError
        from mp4timeline.mp4parse.sample_table import build_samples

        with pytest.raises(SampleTableError, match="stsc"):
            build_samples(
                stts=[(2, 100)],
                ctts=None,
                stsc=[(1, 3, 1)],  # 声称每 chunk 3 样本
                sizes=[10, 20],
                chunk_offsets=[0],
                sync_numbers=None,
            )
