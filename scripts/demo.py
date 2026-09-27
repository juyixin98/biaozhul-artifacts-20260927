"""本地演示脚本：不依赖网络，直接跑全部场景并打印可读结论。

用法::

    python scripts/demo.py            # 跑全部场景
    python scripts/demo.py clock_drift pause_resume
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import __version__
from app.core.scenarios import SCENARIOS


def _bar(n: int, total: int, width: int = 24) -> str:
    total = max(total, 1)
    filled = int(round(width * n / total))
    return "█" * filled + "·" * (width - filled)


def main(argv: list[str]) -> int:
    names = argv[1:] or list(SCENARIOS)
    unknown = [n for n in names if n not in SCENARIOS]
    if unknown:
        print(f"未知场景: {unknown}；可选 {sorted(SCENARIOS)}")
        return 2

    print(f"离线 RTP 抖动缓冲后端  v{__version__}  本地演示\n")
    all_ok = True
    for name in names:
        report = SCENARIOS[name]()
        a, f = report.arms["adaptive"], report.arms["fixed"]
        status = "PASS" if report.passed else "FAIL"
        all_ok &= report.passed
        print(f"[{status}] {name} —— {report.description}")
        for arm_label, st in (("自适应", a), ("固定40ms", f)):
            total = st.frames or 1
            ok_frac = st.audio / total
            losses = st.gaps + st.drops["late_after_playout"]
            print(f"    {arm_label:>7} 音频 {st.audio:>4}  空缺 {st.gaps:>3}  "
                  f"丢弃(晚到) {st.drops['late_after_playout']:>3}  "
                  f"溢出 {st.drops['overflow']:>3}  重复 {st.drops['duplicate']:>3}  "
                  f"峰值缓冲 {st.peak_occupancy:>3}  漂移比 {st.final_ratio:.4f}")
            print(f"           播放占比 {ok_frac:5.1%} {_bar(st.audio, total)}")
        print("    断言:")
        for row in report.assertions:
            mark = {"pass": "✓", "fail": "✗", "uncertain": "~"}[row["status"]]
            print(f"      {mark} {row['id']}: {row['detail']}")
        for note in report.notes:
            print(f"      · {note}")
        print()

    print("=" * 70)
    print("总体：全部通过" if all_ok else "总体：存在失败断言")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
