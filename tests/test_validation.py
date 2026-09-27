"""证据校验套件自身的测试：全通过、载荷篡改可检出、缺夹具记 error。"""

import json
from pathlib import Path

import pytest

from mp4timeline.validation.checks import run_fixture_check, run_validation


def test_all_handwritten_references_match(generated_dir, testlog):
    """用全部手写参考跑 run_validation：overall 必须为 pass。"""

    references_dir = Path(__file__).resolve().parent.parent / "fixtures" / "references"
    report = run_validation(generated_dir, references_dir)
    failures = []
    for fixture in report["fixtures"]:
        for check in fixture["checks"]:
            if check["status"] != "pass":
                failures.append((fixture["fixture"], check["name"], check))
    testlog.write(
        {
            "event": "assertion",
            "test": "handwritten_references",
            "basis": "run_validation 对照 fixtures/references/*.json（手写）+ payloads.json（构建器）",
            "expected": "overall=pass",
            "actual": report["overall"],
        }
    )
    assert not failures, failures
    assert report["overall"] == "pass"


def test_tampered_payload_is_detected(generated_dir, tmp_path):
    """篡改 mdat 中一个样本字节，字节范围 sha1 复核必须判 fail。"""

    references_dir = Path(__file__).resolve().parent.parent / "fixtures" / "references"
    reference = json.loads((references_dir / "bframes.json").read_text())
    payloads = json.loads((generated_dir / "payloads.json").read_text())["bframes.mp4"]

    corrupted = tmp_path / "bframes.mp4"
    raw = bytearray((generated_dir / "bframes.mp4").read_bytes())
    # 在 mdat 载荷区中部翻转一个字节（属于某个样本范围）
    mdat_pos = raw.find(b"mdat") + 4
    raw[mdat_pos + 10] ^= 0xFF
    corrupted.write_bytes(raw)

    report = run_fixture_check(corrupted, reference, payloads)
    byte_check = next(c for c in report.checks if c.name == "track1.byte_ranges")
    assert byte_check.status == "fail"
    assert report.status == "fail"


def test_unparseable_fixture_is_error_not_success(generated_dir, tmp_path):
    references_dir = Path(__file__).resolve().parent.parent / "fixtures" / "references"
    reference = json.loads((references_dir / "bframes.json").read_text())
    broken = tmp_path / "bframes.mp4"
    broken.write_bytes((generated_dir / "bad_length.mp4").read_bytes())

    report = run_fixture_check(broken, reference, None)
    assert report.status == "error"
    assert report.checks[0].actual.startswith("BoxLengthError")


def test_missing_fixture_dir_is_error(tmp_path):
    references_dir = Path(__file__).resolve().parent.parent / "fixtures" / "references"
    report = run_validation(tmp_path / "empty", references_dir)
    assert report["overall"] == "error"


def test_no_references_is_error(generated_dir, tmp_path):
    report = run_validation(generated_dir, tmp_path)
    assert report["overall"] == "error"
