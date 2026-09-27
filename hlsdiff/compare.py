"""相邻版本对比。

判定规则(按媒体序号对齐,与时间线无关):
- 旧版本中序号 < 新版本 MEDIA-SEQUENCE 的分段:滑动窗口前移丢弃,属正常滚动,
  不得判为内容撤回;
- 旧版本中序号 >= 新版本 MEDIA-SEQUENCE 却在新版本缺失:窗口内内容撤回 → reject;
- 同一已见序号 URI 或时长不一致:冲突,单列 → conflict;
- 新版本出现旧版本没有的序号:追加;若旧版本已 ENDLIST → reject(结束后追加);
- discontinuity 序号变化只作报告,不影响 accept/reject。
"""

from __future__ import annotations

from .errors import FailureCategory
from .models import CompareReport, Playlist, SegmentConflict

_DURATION_TOLERANCE = 1e-3  # 浮点时长的判等容差(秒)


def compare_versions(old: Playlist, new: Playlist) -> CompareReport:
    report = CompareReport(decision="accept")
    old_by_seq = old.by_sequence()
    new_by_seq = new.by_sequence()

    for seq in sorted(old_by_seq):
        if seq < new.media_sequence:
            report.window_advanced.append(seq)
        elif seq not in new_by_seq:
            report.retracted.append(seq)

    for seq in sorted(new_by_seq):
        if seq not in old_by_seq:
            report.appended.append(seq)

    for seq in sorted(old_by_seq.keys() & new_by_seq.keys()):
        o, n = old_by_seq[seq], new_by_seq[seq]
        if o.uri != n.uri:
            report.conflicts.append(
                SegmentConflict(seq, "uri", _redact(o.uri), _redact(n.uri))
            )
        if abs(o.duration - n.duration) > _DURATION_TOLERANCE:
            report.conflicts.append(
                SegmentConflict(seq, "duration", str(o.duration), str(n.duration))
            )

    if old.discontinuity_sequence != new.discontinuity_sequence:
        report.discontinuity_shift = (old.discontinuity_sequence, new.discontinuity_sequence)

    # 判定:reject 优先于 conflict
    if old.endlist and report.appended:
        report.decision = "reject"
        report.reasons.append(
            f"{FailureCategory.APPEND_AFTER_ENDLIST.value}: 旧版本已 ENDLIST,"
            f"新版本仍追加序号 {report.appended}"
        )
    if report.retracted:
        report.decision = "reject"
        report.reasons.append(
            f"{FailureCategory.CONTENT_RETRACTED.value}: 窗口内序号 {report.retracted} 被撤回"
        )
    if report.decision != "reject" and report.conflicts:
        report.decision = "conflict"
        report.reasons.append(
            f"{FailureCategory.SEGMENT_CONFLICT.value}: "
            f"{len(report.conflicts)} 处已见序号 URI/时长不一致"
        )
    if report.decision == "accept":
        report.reasons.append(
            f"窗口前移 {len(report.window_advanced)} 段,追加 {len(report.appended)} 段,"
            "重叠区一致"
        )
    return report


def _redact(uri: str) -> str:
    """诊断中的 URI 脱敏:去掉查询串,避免泄露签名令牌。"""
    path, sep, _ = uri.partition("?")
    return path + ("?<redacted>" if sep else "")
