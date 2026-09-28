"""脱敏测试。"""
from colaudit.masking import REDACTED, mask_row, mask_stats_dict


def test_sensitive_stats_redact_endpoints_keep_structure():
    stats = {
        "logical_type": "string",
        "count": 10,
        "null_count": 2,
        "nan_count": 0,
        "has_positive_zero": False,
        "has_negative_zero": False,
        "min_truncated": True,
        "max_truncated": False,
        "sorted": "asc",
        "min": "secret-alice",
        "max": "secret-bob",
    }
    masked = mask_stats_dict(stats, sensitive=True)
    assert masked["min"] == REDACTED and masked["max"] == REDACTED
    # 结构信息保留
    assert masked["count"] == 10
    assert masked["null_count"] == 2
    assert masked["min_truncated"] is True
    assert masked["sorted"] == "asc"
    # 非敏感列原样
    plain = mask_stats_dict(stats, sensitive=False)
    assert plain["min"] == "secret-alice"


def test_mask_row():
    row = mask_row(
        {"id": 1, "name": "alice", "score": 3.5},
        {"name"},
    )
    assert row == {"id": 1, "name": REDACTED, "score": 3.5}
    # NULL 不打码 (NULL 本身不泄露)
    row2 = mask_row({"name": None}, {"name"})
    assert row2["name"] is None
