"""GF(2^8) 域参数与运算测试：库实现必须与 FIPS-197 已知答案一致。"""
from __future__ import annotations

import pytest
from pyfinite import ffield

from threshold_service import gf
from tests import oracle_reference as oracle


def test_fips197_known_vectors_match_library():
    field = gf.get_field(gf.FieldParams())
    for (a, b), expected in oracle.FIPS197_VECTORS.items():
        assert field.Multiply(a, b) == expected
        assert oracle.gf_mul(a, b) == expected  # 独立实现互相印证


def test_inverses_and_division_roundtrip():
    field = gf.get_field(gf.FieldParams())
    for a in range(1, 256):
        assert field.Multiply(a, field.Inverse(a)) == 1
        assert gf.div(gf.FieldParams(), a, a) == 1


def test_unsupported_field_is_refused():
    params = gf.FieldParams(bits=8, generator=0x11D)
    with pytest.raises(gf.UnsupportedField):
        gf.get_field(params)


def test_field_params_identity_string_is_stable():
    assert gf.FieldParams().canonical() == "GF(2^8)/0x11B"
    # 显式确认我们绑定的不是 pyfinite 默认生成元（默认 0x11D，无 LUT 缓存）：
    assert ffield.FField(8, useLUT=0).generator == 0x11D
