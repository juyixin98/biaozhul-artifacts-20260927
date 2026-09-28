"""版本对比测试：滑动窗口、撤回、冲突、ENDLIST 规则。"""
from hlsplan.compare import compare_versions
from hlsplan.diagnostics import DiagnosticLog
from hlsplan.models import FailureCategory
from hlsplan.parser import parse_playlist


def _load(fixture_text, name):
    return parse_playlist(fixture_text(name))


def test_window_advance_is_not_retraction(fixture_text):
    old = _load(fixture_text, "window_v1.m3u8")
    new = _load(fixture_text, "window_v2.m3u8")
    diff = compare_versions(old, new)
    # 手工答案：100/101 滑出窗口（正常淘汰），105/106 新增，无撤回、无冲突
    assert diff.window_advanced_by == 2
    assert diff.expired == [100, 101]
    assert diff.retracted == []
    assert diff.appended == [105, 106]
    assert diff.conflicts == []
    assert diff.rejected is False
    assert diff.window_rewind is False


def test_in_window_disappearance_is_retraction(fixture_text):
    old = _load(fixture_text, "window_v1.m3u8")
    new = _load(fixture_text, "missing_v2.m3u8")
    log = DiagnosticLog()
    diff = compare_versions(old, new, log=log)
    # 103 >= 新窗口起点 102 却消失 -> 撤回，而非窗口淘汰
    assert diff.retracted == [103]
    assert diff.expired == [100, 101]
    codes = {r.code for r in log.records}
    assert FailureCategory.SEGMENT_RETRACTED.value in codes


def test_seen_sequence_conflicts_listed_separately(fixture_text):
    old = _load(fixture_text, "window_v1.m3u8")
    new = _load(fixture_text, "conflict_v2.m3u8")
    diff = compare_versions(old, new)
    kinds = {(c.media_sequence, c.kind) for c in diff.conflicts}
    # 手工答案：103 换了 URI，104 时长 4.0 -> 5.0
    assert (103, "URI_CONFLICT") in kinds
    assert (104, "DURATION_CONFLICT") in kinds
    dur = next(c for c in diff.conflicts if c.kind == "DURATION_CONFLICT")
    assert (dur.old_value, dur.new_value) == (4.0, 5.0)
    # 冲突 URI 已脱敏，不含 token
    uri_c = next(c for c in diff.conflicts if c.kind == "URI_CONFLICT")
    assert "token" not in str(uri_c.new_value)
    assert uri_c.new_value == "seg103_replaced.ts"
    # 冲突不算撤回也不算新增
    assert 103 not in diff.retracted and 103 not in diff.appended


def test_append_after_endlist_rejected(fixture_text):
    old = _load(fixture_text, "endlist_v1.m3u8")
    new = _load(fixture_text, "endlist_v2_append.m3u8")
    log = DiagnosticLog()
    diff = compare_versions(old, new, log=log)
    assert diff.rejected is True
    assert diff.appended == [3]
    assert any("ENDLIST" in r for r in diff.reject_reasons)
    codes = {r.code for r in log.records if r.severity == "ERROR"}
    assert FailureCategory.APPEND_AFTER_ENDLIST.value in codes


def test_window_rewind_flagged(fixture_text):
    old = _load(fixture_text, "window_v2.m3u8")
    new = _load(fixture_text, "window_v1.m3u8")
    diff = compare_versions(old, new)
    assert diff.window_rewind is True
    assert diff.window_advanced_by == 0
