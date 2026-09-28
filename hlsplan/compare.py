"""版本对比：滑动窗口、内容撤回、序号冲突与 ENDLIST 规则。

核心区分：
- 序号 < 新版本 media_sequence 的消失 = 窗口前移（正常，expired）；
- 序号 >= 新版本 media_sequence 却不在新窗口内 = 内容撤回（异常，retracted）；
- 两版都出现的序号但 URI / 时长不一致 = 冲突，单列（conflicts），
  不与"撤回"或"新增"混淆；
- 旧版已 ENDLIST 而新版仍追加分段 -> 拒绝（APPEND_AFTER_ENDLIST）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from .diagnostics import DiagnosticLog, redact_uri
from .models import FailureCategory, PlaylistSnapshot


@dataclass
class SegmentConflict:
    media_sequence: int
    kind: str  # URI_CONFLICT | DURATION_CONFLICT | DISCONTINUITY_SEQUENCE_CONFLICT
    old_value: object
    new_value: object

    def to_dict(self) -> dict:
        return {
            "media_sequence": self.media_sequence,
            "kind": self.kind,
            "old_value": self.old_value,
            "new_value": self.new_value,
        }


@dataclass
class VersionDiff:
    from_version: int
    to_version: int
    window_advanced_by: int = 0
    expired: List[int] = field(default_factory=list)      # 窗口前移淘汰（正常）
    retracted: List[int] = field(default_factory=list)    # 窗口内消失（异常）
    appended: List[int] = field(default_factory=list)     # 新增序号
    conflicts: List[SegmentConflict] = field(default_factory=list)
    endlist_before: bool = False
    endlist_after: bool = False
    window_rewind: bool = False
    rejected: bool = False
    reject_reasons: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "from_version": self.from_version,
            "to_version": self.to_version,
            "window_advanced_by": self.window_advanced_by,
            "expired": self.expired,
            "retracted": self.retracted,
            "appended": self.appended,
            "conflicts": [c.to_dict() for c in self.conflicts],
            "endlist_before": self.endlist_before,
            "endlist_after": self.endlist_after,
            "window_rewind": self.window_rewind,
            "rejected": self.rejected,
            "reject_reasons": self.reject_reasons,
        }


def compare_versions(
    old: PlaylistSnapshot,
    new: PlaylistSnapshot,
    duration_tolerance: float = 0.001,
    log: Optional[DiagnosticLog] = None,
) -> VersionDiff:
    log = log or DiagnosticLog()
    diff = VersionDiff(
        from_version=old.version,
        to_version=new.version,
        endlist_before=old.endlist,
        endlist_after=new.endlist,
    )

    old_by_seq = {s.media_sequence: s for s in old.segments}
    new_by_seq = {s.media_sequence: s for s in new.segments}

    # --- 窗口位置 ---
    if new.media_sequence < old.media_sequence:
        diff.window_rewind = True
        log.warning(
            FailureCategory.WINDOW_REWIND.value,
            "media sequence moved backwards",
            old_media_sequence=old.media_sequence,
            new_media_sequence=new.media_sequence,
        )
    diff.window_advanced_by = max(0, new.media_sequence - old.media_sequence)

    # --- 消失的分段：窗口淘汰 vs 内容撤回 ---
    # 撤回包括两种：序号 >= 新窗口起点却消失；或序号仍在但被 EXT-X-GAP 占位
    for seq in sorted(old_by_seq):
        o = old_by_seq[seq]
        n = new_by_seq.get(seq)
        if n is not None and not n.gap:
            continue
        if o.gap:
            continue  # 本来就是缺口，状态未变
        if n is None and seq < new.media_sequence:
            diff.expired.append(seq)  # 滑出窗口，不是撤回
        else:
            diff.retracted.append(seq)
            log.warning(
                FailureCategory.SEGMENT_RETRACTED.value,
                "segment disappeared inside the live window"
                + (" (became GAP)" if n is not None else ""),
                media_sequence=seq,
                segment_uri=o.uri,
            )

    # --- 新增分段（GAP 占位不算可下载新增） ---
    old_last = old.last_sequence
    for seq in sorted(new_by_seq):
        if new_by_seq[seq].gap:
            continue
        if seq not in old_by_seq and (old_last is None or seq > old_last):
            diff.appended.append(seq)

    # --- 已见序号冲突（单列；GAP 占位不参与内容比对） ---
    for seq in sorted(set(old_by_seq) & set(new_by_seq)):
        o, n = old_by_seq[seq], new_by_seq[seq]
        if o.gap or n.gap:
            continue
        if o.uri != n.uri:
            diff.conflicts.append(
                SegmentConflict(seq, "URI_CONFLICT", redact_uri(o.uri), redact_uri(n.uri))
            )
        if abs(o.duration - n.duration) > duration_tolerance:
            diff.conflicts.append(
                SegmentConflict(seq, "DURATION_CONFLICT", o.duration, n.duration)
            )
        if o.discontinuity_sequence != n.discontinuity_sequence:
            diff.conflicts.append(
                SegmentConflict(
                    seq,
                    "DISCONTINUITY_SEQUENCE_CONFLICT",
                    o.discontinuity_sequence,
                    n.discontinuity_sequence,
                )
            )
    for c in diff.conflicts:
        log.warning(
            FailureCategory.SEGMENT_CONFLICT.value,
            f"{c.kind} at media sequence {c.media_sequence}",
            media_sequence=c.media_sequence,
            kind=c.kind,
        )

    # --- ENDLIST 规则：结束后追加应拒绝 ---
    if old.endlist and diff.appended:
        diff.rejected = True
        reason = (
            f"append after ENDLIST rejected: {len(diff.appended)} new segments "
            f"{diff.appended}"
        )
        diff.reject_reasons.append(reason)
        log.error(
            FailureCategory.APPEND_AFTER_ENDLIST.value,
            reason,
            appended=diff.appended,
        )

    if not diff.rejected:
        log.info(
            "DIFF_ACCEPTED",
            "version diff computed",
            expired=len(diff.expired),
            retracted=len(diff.retracted),
            appended=len(diff.appended),
            conflicts=len(diff.conflicts),
        )
    return diff
