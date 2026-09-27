"""Generate reusable on-disk fixtures and independently derived manifests.

Run:  python -m scripts.make_fixtures

Writes *.ts and *.expected.json pairs into tests/fixtures/generated/.
The JSON manifest states the expected map / anomaly ledger *by construction*
of the independent builder -- it never imports the analyzer.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.tsbuilder import (  # noqa: E402
    PACKET_SIZE,
    PAT_PID,
    StreamBuilder,
    corrupt_crc,
    pat_section,
    pmt_section,
    pes_packet,
    pes_packets,
    section_packets,
    ts_packet,
)

OUT = ROOT / "tests" / "fixtures" / "generated"

PMT_PID = 0x1000
VIDEO_PID = 0x1100
AUDIO_PID = 0x1101
PROGRAM = 0x300


def _write(name: str, data: bytes, manifest: dict) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"{name}.ts").write_bytes(data)
    (OUT / f"{name}.expected.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True))
    print(f"wrote {name}.ts ({len(data)} bytes)")


def _base_tables(sb: StreamBuilder, pat_version: int = 1,
                 pmt_version: int = 1) -> None:
    pat = pat_section({PROGRAM: PMT_PID}, version=pat_version)
    for p in section_packets(PAT_PID, pat, start_cc=sb.cc.get(0, 0)):
        sb.add(p, 0)
    sb.cc[0] = (sb.cc.get(0, 0) + 2) % 16 if len(pat) > 183 else \
        (sb.cc.get(0, 0) + 1) % 16
    pmt = pmt_section(PROGRAM, PMT_PID, [(0x1B, VIDEO_PID), (0x0F, AUDIO_PID)],
                      version=pmt_version, pcr_pid=VIDEO_PID)
    cc = sb.cc.get(PMT_PID, 0)
    for p in section_packets(PMT_PID, pmt, start_cc=cc):
        sb.add(p, PMT_PID)
    sb.cc[PMT_PID] = (cc + (1 if len(pmt) <= 184 else 2)) % 16


def fixture_baseline() -> None:
    sb = StreamBuilder()
    _base_tables(sb)
    # Two video PES and one audio PES.
    for i, pts in enumerate((9000, 27000)):
        pes = pes_packet(0xE0, bytes([0xA0 | i]) * 300, pts=pts)
        start = sb.packet_count
        for p in pes_packets(VIDEO_PID, pes,
                             start_cc=sb.cc.get(VIDEO_PID, 0)):
            sb.add(p, VIDEO_PID)
        n = (len(pes) + 183) // 184
        sb.cc[VIDEO_PID] = (sb.cc.get(VIDEO_PID, 0) + n) % 16
        sb.expected_pes.append(
            {"pid": VIDEO_PID, "start_packet_index": start,
             "length": len(pes), "pts": pts, "dts": None, "corrupt": False})
    pes = pes_packet(0xC0, b"\xAA" * 200, pts=8000)
    start = sb.packet_count
    for p in pes_packets(AUDIO_PID, pes, start_cc=sb.cc.get(AUDIO_PID, 0)):
        sb.add(p, AUDIO_PID)
    n = (len(pes) + 183) // 184
    sb.cc[AUDIO_PID] = (sb.cc.get(AUDIO_PID, 0) + n) % 16
    sb.expected_pes.append(
        {"pid": AUDIO_PID, "start_packet_index": start,
         "length": len(pes), "pts": 8000, "dts": None, "corrupt": False})

    _write("baseline", sb.build(), {
        "parsed_packets": sb.packet_count,
        "verdict": "accepted",
        "programs": {
            str(PROGRAM): {
                "pmt_pid": PMT_PID,
                "streams": [
                    {"elementary_pid": VIDEO_PID, "stream_type": 0x1B},
                    {"elementary_pid": AUDIO_PID, "stream_type": 0x0F},
                ]}},
        "expected_pes": sb.expected_pes,
        "expected_finding_codes": ["cc_baseline", "cc_baseline",
                                   "cc_baseline", "cc_baseline",
                                   "cc_baseline"],
        "resyncs": 0,
        "trailing_bytes": 0,
    })


def fixture_duplicate() -> None:
    sb = StreamBuilder()
    _base_tables(sb)
    pes = pes_packet(0xE0, b"\x11" * 250, pts=9000)
    pkts = pes_packets(VIDEO_PID, pes, start_cc=sb.cc.get(VIDEO_PID, 0))
    start = sb.packet_count
    sb.add(pkts[0], VIDEO_PID)
    sb.add_duplicate(VIDEO_PID, pkts[0])   # exact repeat
    sb.add(pkts[1], VIDEO_PID)
    sb.cc[VIDEO_PID] = 2
    sb.expected_pes.append(
        {"pid": VIDEO_PID, "start_packet_index": start,
         "length": len(pes), "pts": 9000, "dts": None, "corrupt": False})
    _write("duplicate", sb.build(), {
        "parsed_packets": sb.packet_count,
        "programs": {str(PROGRAM): {"pmt_pid": PMT_PID}},
        "expected_ledger": [
            {"kind": "duplicate_packet", "pid": VIDEO_PID,
             "packet_index": start + 1}],
        "expected_pes": sb.expected_pes,
    })


def fixture_gap() -> None:
    sb = StreamBuilder()
    _base_tables(sb)
    # A long PES spanning several TS packets; the CC gap lands INSIDE it.
    pes1 = pes_packet(0xE0, b"\x22" * 1000, pts=9000)
    pkts1 = pes_packets(VIDEO_PID, pes1, start_cc=0)
    assert len(pkts1) >= 6
    start1 = sb.packet_count
    sb.add(pkts1[0], VIDEO_PID)   # PUSI cc=0
    sb.add(pkts1[1], VIDEO_PID)   # continuation cc=1
    # No bytes are removed (the reassembled PES stays intact), but the
    # arriving packet's CC jumps 1 -> 5: an unsignaled gap of 3.
    gap_idx = sb.packet_count
    p_after = bytearray(pkts1[2])
    p_after[3] = (p_after[3] & 0xF0) | 5
    sb.add(bytes(p_after), VIDEO_PID)
    sb.ledger.append(("cc_gap", VIDEO_PID, gap_idx,
                      {"missing_estimate": 3}))
    for k in range(3, len(pkts1)):
        p = bytearray(pkts1[k])
        p[3] = (p[3] & 0xF0) | ((5 + k - 2) % 16)
        sb.add(bytes(p), VIDEO_PID)
    # A subsequent healthy PES reassembles normally.
    pes2 = pes_packet(0xE0, b"\x33" * 200, pts=36000)
    start2 = sb.packet_count
    cc2 = (5 + len(pkts1) - 2) % 16
    for p in pes_packets(VIDEO_PID, pes2, start_cc=cc2):
        sb.add(p, VIDEO_PID)
    _write("gap", sb.build(), {
        "parsed_packets": sb.packet_count,
        "programs": {str(PROGRAM): {"pmt_pid": PMT_PID}},
        "expected_ledger": [
            {"kind": "cc_gap", "pid": VIDEO_PID,
             "packet_index": gap_idx, "missing_estimate": 3}],
        "expected_pes": [
            {"pid": VIDEO_PID, "start_packet_index": start1,
             "corrupt": True},
            {"pid": VIDEO_PID, "start_packet_index": start2,
             "length": len(pes2), "corrupt": False, "pts": 36000}],
        "pes_gap_count": 1,
    })


def fixture_adaptation_and_signaled() -> None:
    sb = StreamBuilder()
    _base_tables(sb)
    # A complete bounded PES (cc 0..1) before the discontinuity point.
    pes0 = pes_packet(0xE0, b"\x44" * 100, pts=9000)
    start0 = sb.packet_count
    for p in pes_packets(VIDEO_PID, pes0, start_cc=0):
        sb.add(p, VIDEO_PID)
    n0 = (len(pes0) + 183) // 184
    sb.cc[VIDEO_PID] = n0
    # adaptation-only (random_access) legally repeats the last CC.
    last_cc = sb.cc[VIDEO_PID] - 1
    p = ts_packet(VIDEO_PID, b"", cc=last_cc, random_access=True)
    sb.add(p, VIDEO_PID)
    # Signaled discontinuity: adaptation-only carries the indicator and
    # repeats the CC; the counter baseline restarts on the next payload.
    p_di = ts_packet(VIDEO_PID, b"", cc=last_cc, discontinuity=True,
                     random_access=True, pcr=0)
    di_idx = sb.add(p_di, VIDEO_PID)
    # A fresh, complete PES begins the new continuity run at cc=0.
    pes1 = pes_packet(0xE0, b"\x55" * 120, pts=27000)
    signaled_idx = sb.packet_count
    for p in pes_packets(VIDEO_PID, pes1, start_cc=0):
        sb.add(p, VIDEO_PID)
    sb.ledger.append(("signaled_discontinuity", VIDEO_PID, signaled_idx,
                      {"restart_cc": 0}))
    sb.expected_pes.append(
        {"pid": VIDEO_PID, "start_packet_index": start0,
         "length": len(pes0), "pts": 9000, "corrupt": False})
    sb.expected_pes.append(
        {"pid": VIDEO_PID, "start_packet_index": signaled_idx,
         "length": len(pes1), "pts": 27000, "corrupt": False})
    _write("adaptation_signaled", sb.build(), {
        "parsed_packets": sb.packet_count,
        "programs": {str(PROGRAM): {"pmt_pid": PMT_PID}},
        "expected_ledger": [
            {"kind": "signaled_discontinuity", "pid": VIDEO_PID,
             "packet_index": signaled_idx}],
        "adaptation_only_packet_indexes": [signaled_idx - 2,
                                           signaled_idx - 1],
        "expected_pes": sb.expected_pes,
    })


def fixture_cross_packet_pmt() -> None:
    sb = StreamBuilder()
    pat = pat_section({PROGRAM: PMT_PID}, version=1)
    for p in section_packets(0x0000, pat, start_cc=0):
        sb.add(p, 0)
    sb.cc[0] = 1
    # PMT deliberately larger than one TS payload so it spans packets with
    # standard capacity-aligned fragmentation (no fixed-PID assumption: the
    # video PID is learned only after this multi-packet PMT is committed).
    many_streams = [(0x1B if i == 0 else 0x80, VIDEO_PID + i)
                    for i in range(40)]
    many_streams[1] = (0x0F, AUDIO_PID)
    pmt = pmt_section(PROGRAM, PMT_PID, many_streams,
                      version=1, pcr_pid=VIDEO_PID)
    pkts = section_packets(PMT_PID, pmt, start_cc=0)
    split_indexes = []
    for j, p in enumerate(pkts):
        split_indexes.append(sb.packet_count)
        sb.add(p, PMT_PID)
    pes = pes_packet(0xE0, b"\x66" * 150, pts=9000)
    start = sb.packet_count
    for p in pes_packets(VIDEO_PID, pes, start_cc=0):
        sb.add(p, VIDEO_PID)
    sb.expected_pes.append(
        {"pid": VIDEO_PID, "start_packet_index": start,
         "length": len(pes), "pts": 9000, "corrupt": False})
    _write("cross_packet_pmt", sb.build(), {
        "parsed_packets": sb.packet_count,
        "pmt_section_bytes": len(pmt),
        "pmt_split_packet_indexes": split_indexes,
        "programs": {
            str(PROGRAM): {
                "pmt_pid": PMT_PID,
                "streams": [
                    {"elementary_pid": VIDEO_PID, "stream_type": 0x1B},
                    {"elementary_pid": AUDIO_PID, "stream_type": 0x0F}]}},
        "expected_pes": sb.expected_pes,
        "expected_finding_codes_absent": ["section_crc_error",
                                          "section_malformed"],
    })


def fixture_bad_crc() -> None:
    sb = StreamBuilder()
    pat = pat_section({PROGRAM: PMT_PID}, version=1)
    for p in section_packets(0x0000, pat, start_cc=0):
        sb.add(p, 0)
    sb.cc[0] = 1
    good_pmt = pmt_section(PROGRAM, PMT_PID, [(0x1B, VIDEO_PID)],
                           version=1, pcr_pid=VIDEO_PID)
    bad_pmt = corrupt_crc(good_pmt)
    crc_idx = sb.packet_count
    for p in section_packets(PMT_PID, bad_pmt, start_cc=0):
        sb.add(p, PMT_PID)
    # Payload on the would-be video PID must remain unclassifiable.
    unknown_idx = sb.add_payload_packet(VIDEO_PID, b"\x77" * 100, pusi=True)
    _write("bad_crc", sb.build(), {
        "parsed_packets": sb.packet_count,
        "programs": {},  # no PMT map may be installed
        "expected_ledger": [
            {"kind": "section_crc_error", "pid": PMT_PID,
             "packet_index": crc_idx}],
        "unknown_pid_packet_index": unknown_idx,
    })


def fixture_version_switch() -> None:
    sb = StreamBuilder()
    _base_tables(sb, pat_version=1, pmt_version=1)
    # PAT v2 removes audio by pointing at a new PMT version with video only.
    pat2 = pat_section({PROGRAM: PMT_PID}, version=2)
    for p in section_packets(0x0000, pat2, start_cc=sb.cc[0]):
        sb.add(p, 0)
    sb.cc[0] = (sb.cc[0] + 1) % 16
    pmt2 = pmt_section(PROGRAM, PMT_PID, [(0x1B, VIDEO_PID)],
                       version=2, pcr_pid=VIDEO_PID)
    for p in section_packets(PMT_PID, pmt2, start_cc=sb.cc[PMT_PID]):
        sb.add(p, PMT_PID)
    sb.cc[PMT_PID] = (sb.cc[PMT_PID] + 1) % 16
    _write("version_switch", sb.build(), {
        "parsed_packets": sb.packet_count,
        "pat_version_after": 2,
        "pmt_version_after": 2,
        "streams_after": [{"elementary_pid": VIDEO_PID, "stream_type": 0x1B}],
        "expected_finding_codes": ["table_version_switch",
                                   "table_version_switch"],
    })


def fixture_resync() -> None:
    sb = StreamBuilder()
    _base_tables(sb)
    pes = pes_packet(0xE0, b"\x88" * 184, pts=9000)
    start = sb.packet_count
    for p in pes_packets(VIDEO_PID, pes, start_cc=0):
        sb.add(p, VIDEO_PID)
    sb.cc[VIDEO_PID] = (len(pes) + 183) // 184
    garbage_at = sb.packet_count * PACKET_SIZE
    sb.add_raw(b"\x00\xAB\xCD" + b"\x12" * 7)  # no 0x47 lock candidate
    # After resync a fresh PES with PUSI starts a new continuity baseline.
    pes2 = pes_packet(0xE0, b"\x99" * 160, pts=36000)
    resync_idx = sb.packet_count
    for p in pes_packets(VIDEO_PID, pes2, start_cc=0):
        sb.add(p, VIDEO_PID)
    sb.expected_pes.append(
        {"pid": VIDEO_PID, "start_packet_index": start,
         "length": len(pes), "pts": 9000, "corrupt": False})
    sb.expected_pes.append(
        {"pid": VIDEO_PID, "start_packet_index": resync_idx,
         "length": len(pes2), "pts": 36000, "corrupt": False})
    _write("resync", sb.build(), {
        "garbage_byte_offset": garbage_at,
        "garbage_bytes": 10,
        "expected_ledger": [
            {"kind": "resync_occurred", "packet_index_after": resync_idx}],
        "programs": {str(PROGRAM): {"pmt_pid": PMT_PID}},
        "expected_pes": sb.expected_pes,
    })


def main() -> None:
    fixture_baseline()
    fixture_duplicate()
    fixture_gap()
    fixture_adaptation_and_signaled()
    fixture_cross_packet_pmt()
    fixture_bad_crc()
    fixture_version_switch()
    fixture_resync()
    print(f"fixtures written to {OUT}")


if __name__ == "__main__":
    main()
