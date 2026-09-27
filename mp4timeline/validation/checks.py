"""证据校验套件。

对每个有手写参考时间表（fixtures/references/*.json）的夹具：

1. 重新解析并构建时间线；
2. 逐项对照参考值（DTS/CTO/PTS、movie_time、播放区间、未呈现样本）；
3. 用夹具构建器独立记录的样本载荷 sha1 复核解析器给出的字节范围。

判定原则：
- 参考时间表是手工编写的，不由被测核心生成；
- 任何一步抛异常 → 该检查 status=error（带异常类别），绝不记为 pass；
- 全部检查 pass 时 overall 才是 pass。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from ..errors import Mp4TimelineError
from ..jobs.runner import serialize_movie
from ..mp4parse.parser import parse_movie
from ..timeline.kernel import build_track_timeline


@dataclass
class Check:
    name: str
    status: str  # pass | fail | error
    basis: str
    expected: object = None
    actual: object = None

    def view(self) -> dict:
        return {
            "name": self.name,
            "status": self.status,
            "basis": self.basis,
            "expected": self.expected,
            "actual": self.actual,
        }


@dataclass
class FixtureReport:
    fixture: str
    checks: list[Check] = field(default_factory=list)

    @property
    def status(self) -> str:
        if any(c.status == "error" for c in self.checks):
            return "error"
        if any(c.status == "fail" for c in self.checks):
            return "fail"
        return "pass" if self.checks else "error"

    def view(self) -> dict:
        return {
            "fixture": self.fixture,
            "status": self.status,
            "checks": [c.view() for c in self.checks],
        }


def _eq_check(
    report: FixtureReport, name: str, expected, actual, basis: str
) -> None:
    report.checks.append(
        Check(
            name=name,
            status="pass" if expected == actual else "fail",
            basis=basis,
            expected=expected,
            actual=actual,
        )
    )


def run_fixture_check(
    fixture_path: Path, reference: dict, payloads: dict | None
) -> FixtureReport:
    report = FixtureReport(fixture=fixture_path.name)
    basis_ref = f"手写参考 fixtures/references/{fixture_path.stem}.json"

    try:
        data = fixture_path.read_bytes()
        movie = parse_movie(data, source=str(fixture_path))
        timelines = [build_track_timeline(t, movie.movie_timescale) for t in movie.tracks]
        result = serialize_movie(movie, timelines)
    except Mp4TimelineError as exc:
        report.checks.append(
            Check(
                name="parse",
                status="error",
                basis="解析阶段抛异常",
                expected="解析成功",
                actual=f"{type(exc).__name__}: {exc}",
            )
        )
        return report
    except Exception as exc:  # 未预期错误：如实记 error
        report.checks.append(
            Check(
                name="parse",
                status="error",
                basis="解析阶段抛出未预期异常",
                expected="解析成功",
                actual=f"Unexpected:{type(exc).__name__}: {exc}",
            )
        )
        return report

    _eq_check(
        report,
        "movie_header",
        {
            "movie_timescale": reference["movie_timescale"],
            "movie_duration": reference["movie_duration"],
        },
        {
            "movie_timescale": result["movie_timescale"],
            "movie_duration": result["movie_duration"],
        },
        basis_ref,
    )

    for ref_track in reference["tracks"]:
        tid = ref_track["track_id"]
        actual_track = next(
            (t for t in result["tracks"] if t["track_id"] == tid), None
        )
        if actual_track is None:
            report.checks.append(
                Check(f"track{tid}", "fail", basis_ref, "存在该轨", "结果中缺失")
            )
            continue

        _eq_check(
            report,
            f"track{tid}.samples",
            [
                [s["dts"], s["cto"], s["pts"], s["duration"], s["size"], s["is_sync"]]
                for s in ref_track["samples"]
            ],
            [
                [s["dts"], s["cto"], s["pts"], s["duration"], s["byte_range"][1], s["is_sync"]]
                for s in actual_track["samples"]
            ],
            basis_ref,
        )
        _eq_check(
            report,
            f"track{tid}.presentations",
            [
                [p["sample_index"], p["movie_time"], p["movie_duration"]]
                for p in ref_track["presentations"]
            ],
            [
                [p["sample_index"], p["movie_time"], p["movie_duration"]]
                for p in actual_track["presentations"]
            ],
            basis_ref,
        )
        _eq_check(
            report,
            f"track{tid}.unpresented",
            ref_track.get("unpresented", []),
            [u["sample_index"] for u in actual_track["unpresented"]],
            basis_ref,
        )

        # 字节范围复核：sha1 来自夹具构建器（独立于被测核心）。
        if payloads is not None:
            track_payloads = payloads.get(str(tid), {})
            mismatches = []
            for s in actual_track["samples"]:
                off, size = s["byte_range"]
                digest = hashlib.sha1(data[off : off + size]).hexdigest()
                expected_digest = track_payloads.get(str(s["index"]))
                if digest != expected_digest:
                    mismatches.append(s["index"])
            report.checks.append(
                Check(
                    name=f"track{tid}.byte_ranges",
                    status="pass" if not mismatches else "fail",
                    basis="fixtures/generated/payloads.json（构建器独立记录）",
                    expected="全部样本 sha1 与夹具构建器记录一致",
                    actual="一致" if not mismatches else f"不一致样本: {mismatches}",
                )
            )

    return report


def run_validation(fixtures_dir: Path, references_dir: Path) -> dict:
    """运行全部校验，返回汇总报告。"""

    payloads_path = fixtures_dir / "payloads.json"
    payloads_all = (
        json.loads(payloads_path.read_text()) if payloads_path.exists() else {}
    )
    reports: list[FixtureReport] = []

    ref_files = sorted(references_dir.glob("*.json"))
    if not ref_files:
        return {
            "overall": "error",
            "reason": f"参考目录 {references_dir} 中没有任何参考时间表",
            "fixtures": [],
        }

    for ref_path in ref_files:
        reference = json.loads(ref_path.read_text())
        fixture_path = fixtures_dir / reference["fixture"]
        if not fixture_path.exists():
            report = FixtureReport(fixture=reference["fixture"])
            report.checks.append(
                Check(
                    "fixture_exists",
                    "error",
                    "夹具文件必须存在（先运行 fixtures/build_fixtures.py）",
                    str(fixture_path),
                    "不存在",
                )
            )
            reports.append(report)
            continue
        reports.append(
            run_fixture_check(
                fixture_path, reference, payloads_all.get(fixture_path.name)
            )
        )

    overall = "pass"
    if any(r.status == "error" for r in reports):
        overall = "error"
    elif any(r.status == "fail" for r in reports):
        overall = "fail"
    return {
        "overall": overall,
        "fixtures": [r.view() for r in reports],
    }
