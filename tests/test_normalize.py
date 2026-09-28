"""文本规范化层测试：固定版本、硬编码字面值、规范化碰撞。"""

from __future__ import annotations

import pytest

from app.normalize import NORMALIZER_VERSION, canonical_key, normalize_text
from tests.reference import COLLISION_GROUPS, HARDCODED, ref_normalize


class TestNormalizerVersion:
    def test_version_is_fixed_v1(self):
        # 规范化版本必须固定为 norm-v1，不允许悄悄改名
        assert NORMALIZER_VERSION == "norm-v1"

    def test_unknown_version_rejected(self):
        with pytest.raises(ValueError, match="不支持的规范化版本"):
            normalize_text("abc", version="norm-v999")

    def test_normalization_is_idempotent(self):
        for raw, _expected, _note in HARDCODED:
            once = normalize_text(raw)
            twice = normalize_text(once)
            assert once == twice, f"规范化非幂等: {raw!r}"

    def test_display_text_is_preserved_separately(self):
        # 规范化只产出索引键；调用方必须自己保留原文。这里验证
        # 规范化函数不“返回”原文，且原文与键可同时存在。
        raw = "ＣＡＦＥ"
        key = normalize_text(raw)
        assert key == "cafe"
        assert raw != key  # 原文没有被原地替换


class TestNormalizerHardcoded:
    @pytest.mark.parametrize("raw,expected,note", HARDCODED)
    def test_hardcoded_literals(self, raw, expected, note, run_id, log):
        log.info("[%s] normalize 输入=%r 期望=%r (%s)", run_id, raw, expected, note)
        assert normalize_text(raw) == expected, f"失败类别: 规范化字面值不符 ({note})"

    @pytest.mark.parametrize("raw,expected,note", HARDCODED)
    def test_matches_independent_reference(self, raw, expected, note):
        # 与独立重写的参考实现比对（防止被测与参考共用同一错误来源）
        assert ref_normalize(raw) == normalize_text(raw), f"失败类别: 与参考实现不一致 ({note})"


class TestCanonicalCollision:
    @pytest.mark.parametrize("group", COLLISION_GROUPS)
    def test_distinct_displays_collide_to_same_key(self, group):
        keys = {normalize_text(w) for w in group}
        assert len(keys) == 1, f"失败类别: 预期的规范化碰撞未发生: {group} -> {keys}"

    def test_canonical_key_orders_collisions_stably(self):
        # 同规范键、同分时按 display 原文码位序，再按 id 兜底
        a = canonical_key("id-2", "cafe", "CAFE")
        b = canonical_key("id-1", "cafe", "cafe")
        assert a < b  # display 'CAFE'(U+0043) 码位小于 'cafe'(U+0063)，排前

        # display 也相同时 id 决定全序
        c1 = canonical_key("aaa", "cafe", "cafe")
        c2 = canonical_key("bbb", "cafe", "cafe")
        assert c1 < c2

        # 不同规范键首先按规范键
        assert canonical_key("z", "aaa", "x") < canonical_key("a", "bbb", "y")
