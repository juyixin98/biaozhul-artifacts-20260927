#!/usr/bin/env python3
"""黄金向量生成器（**不使用被测核心**）。

有效编码向量全部由成熟 ABI 库 ``eth_abi`` 生成；另手工构造一批畸形
字节串用于断言我们的解码器拒绝并给出正确失败类别。

输出：tests/fixtures/golden_vectors.json
    {
      "generated_by": "eth_abi-... + hand-crafted",
      "valid":   [ {types, value_tag, encoded_hex, decoded_tag}, ... ],
      "malformed":[ {name, types, blob_hex, expected_category}, ... ],
      "selectors":[ {signature, selector_hex, source}, ... ]
    }

value 用可 JSON 表示的标签格式：
    int   -> {"__int__": "-123"}
    bytes -> {"__bytes__": "dead.."}
    tuple -> {"__tuple__": [...]}
    array -> 普通 JSON list
    str   -> 普通 JSON 字符串
"""

from __future__ import annotations

import json
from pathlib import Path

from eth_abi import encode as oracle_encode
from Crypto.Hash import keccak

OUT = Path("tests/fixtures/golden_vectors.json")
WORD = 32


# --------------------------------------------------------------------------
# 值的 JSON 标签化 / 去标签
# --------------------------------------------------------------------------

def tag(value):
    if isinstance(value, bool):
        raise ValueError("本后端不支持 bool")
    if isinstance(value, int):
        return {"__int__": str(value)}
    if isinstance(value, (bytes, bytearray)):
        return {"__bytes__": bytes(value).hex()}
    if isinstance(value, tuple):
        return {"__tuple__": [tag(v) for v in value]}
    if isinstance(value, list):
        return [tag(v) for v in value]
    if isinstance(value, str):
        return value
    raise TypeError(f"无法标签化类型 {type(value)}")


# --------------------------------------------------------------------------
# 有效向量（由 eth_abi 编码）
# --------------------------------------------------------------------------

BIG = 2 ** 255
VALID_CASES = [
    # 整数：边界、负数、符号扩展
    (["uint256"], (0,)),
    (["uint256"], (2 ** 256 - 1,)),
    (["int256"], (0,)),
    (["int256"], (BIG - 1,)),
    (["int256"], (-BIG,)),
    (["int8"], (-1,)),
    (["int8"], (-128,)),
    (["int8"], (127,)),
    (["uint8"], (0,)),
    (["uint8"], (255,)),
    (["int16"], (-256,)),
    (["uint16"], (65535,)),
    (["int256[]"], ([-1, 0, BIG - 1, -BIG],)),
    # 字节
    (["bytes1"], (b"\x00",)),
    (["bytes1"], (b"\xff",)),
    (["bytes20"], (b"\xbe" * 20,)),
    (["bytes32"], (b"\xa5" * 32,)),
    (["bytes"], (b"",)),
    (["bytes"], (b"\x00",)),
    (["bytes"], (b"the quick brown fox",)),
    (["string"], ("",)),
    (["string"], ("a",)),
    (["string"], ("你好，世界 — hello",)),
    # 数组（含嵌套动态）
    (["uint256[]"], ([],)),
    (["uint256[]"], ([1, 2, 3],)),
    (["string[]"], ([],)),
    (["string[]"], (["", "x", "yy"],)),
    (["bytes[]"], (([b"\x11", b"\x22\x33", b""],))),
    (["uint256[3]"], ((1, 2, 3),)),
    (["string[2]"], (("a", "b"),)),
    (["uint16[][2]"], (([1, 2], [3]),)),
    (["string[][2]"], ((["a"], ["b", "cc"]),)),
    (["bytes32[]"], (([b"\x00" * 32, b"\xff" * 32],))),
    # 元组与深层嵌套
    (["(uint256,uint256)"], ((1, 2),)),
    (["(int256,bytes)"], ((-7, b"q"),)),
    (["(string,string)"], (("a", "bb"),)),
    (["(uint256,string)"], ((5, "z"),)),
    (["(bytes,string)"], ((b"\xde\xad", "你好"),)),
    (["(string[],uint256)"], ((["a", "bb"], 7),)),
    (["(uint256,(bytes20,bytes)[])"],
     ((1, [(b"B" * 20, b"x"), (b"C" * 20, b"")]),)),
    (["((string,string),uint256)"], ((("a", "bb"), 9),)),
    (["(uint256,bytes)[]"], ([],)),
    (["(uint256,bytes)[]"], ([(1, b"ab"), (2, b"")],)),
    (["(string,string,string)"], (("", "x", ""),)),
    (["(uint8,(bytes2,uint64))"], ((4, (b"\xab\xcd", 99)),)),
    (["string[][2]"], (([], []),)),
]


def build_valid():
    out = []
    for types, value in VALID_CASES:
        encoded = oracle_encode(types, value)
        out.append({
            "types": types,
            "value": tag(value),
            "encoded_hex": encoded.hex(),
        })
    return out


# --------------------------------------------------------------------------
# 手工畸形向量（期望我们的解码器按类别拒绝）
# --------------------------------------------------------------------------

def w(hexstr: str) -> str:
    return hexstr.replace(" ", "")


def build_malformed():
    m = []

    def add(name, types, blob_hex, category):
        m.append({
            "name": name,
            "types": types,
            "blob_hex": w(blob_hex),
            "expected_category": category,
        })

    # 1. uint8：值右对齐在最右字节；这里把 1 放到最左高位字节（非规范）
    add("uint8_nonzero_high_padding", ["uint8"],
        "01" + "00" * 31, "non_canonical_padding")
    # 2. uint8 声明值 256（0x100），超出 8 位范围
    add("uint8_out_of_range", ["uint8"],
        "0000000000000000000000000000000000000000000000000000000000000100",
        "non_canonical_padding")
    # 3. int8 应为 -1(ff..ff)，高位首字节写成 00（错误符号扩展）
    add("int8_bad_sign_extension", ["int8"],
        "00" + "ff" * 31, "non_canonical_padding")
    # 4. int8 高位字节污染符号位（0x80..7f）
    add("int8_sign_bit_polluted", ["int8"],
        "80" + "00" * 30 + "7f",
        "non_canonical_padding")
    # 5. bytes1 尾部填充非零
    add("bytes32_nonzero_tail_pad", ["bytes1"],
        "ab" + "00" * 30 + "01", "non_canonical_padding")
    # 6. bytes 负载尾部填充非零
    good_bytes = oracle_encode(["bytes"], [b"a"]).hex()
    bad = good_bytes[:-2] + "01"  # 最后一个填充字节置 1
    add("bytes_nonzero_payload_pad", ["bytes"], bad, "non_canonical_padding")
    # 7. 动态偏移指入静态头区（顶层 string，偏移写成 0）
    add("string_offset_into_head", ["string"],
        "00" * 32 + "0000000000000000000000000000000000000000000000000000000000000000",
        "overlap")
    # 8. 动态偏移越界（远超 blob）
    add("string_offset_oob", ["string"],
        "000000000000000000000000000000000000000000000000000000000000ffff",
        "offset_out_of_bounds")
    # 9. 声明巨大长度（防巨大分配）：长度字段超过 1MiB 硬上限，
    #    必须 allocation_limit 拒绝，且不得按长度分配。
    add("bytes_huge_length", ["bytes"],
        ("0000000000000000000000000000000000000000000000000000000000000020"
         "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"),
        "allocation_limit")
    # 10. 动态数组声明巨大元素数
    add("array_huge_count", ["uint256[]"],
        ("0000000000000000000000000000000000000000000000000000000000000020"
         "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"),
        "allocation_limit")
    # 11. 两个动态成员指向同一块（偏移重叠）：(string,string)，
    #     取规范编码后把第二个偏移改成与第一个相同。
    ov = bytearray(oracle_encode(["(string,string)"], [("a", "bb")]))
    ov[32:64] = ov[0:32]  # 第二个偏移槽 := 第一个偏移值
    add("tuple_overlapping_children", ["(string,string)"],
        ov.hex(), "overlap")
    # 12. 动态成员间留出非零间隙（第一个偏移 0x60 而不是 0x40）
    add("tuple_gap_between_children", ["(string,string)"],
        ("0000000000000000000000000000000000000000000000000000000000000060"
         "00000000000000000000000000000000000000000000000000000000000000a0"
         "00" * 32 +
         "0000000000000000000000000000000000000000000000000000000000000001"
         "61" + "00" * 31 +
         "0000000000000000000000000000000000000000000000000000000000000001"
         "62" + "00" * 31),
        "non_canonical_layout")
    # 13. 顶层尾随垃圾字节
    good_int = oracle_encode(["uint256"], [1]).hex()
    add("uint256_trailing_garbage", ["uint256"],
        good_int + "ff", "non_canonical_layout")
    # 14. 截断：uint256 只有 31 字节
    add("uint256_truncated", ["uint256"],
        "00" * 31, "offset_out_of_bounds")
    # 15. 不支持的类型
    add("unsupported_address", ["address"],
        "00" * 32, "unsupported_type")
    add("unsupported_bool", ["bool"],
        "00" * 32, "unsupported_type")
    # 16. bytes 声明长度超出实际块：规范编码 b'a'（长度=1）后把长度字
    #     改成 33；块需要 33+填充=64 字节负载，但该块独占区间只有 32 字节。
    ex = bytearray(oracle_encode(["bytes"], [b"a"]))
    ex[63] = 33  # 长度字：1 -> 33，超出实际可用负载
    add("bytes_length_exceeds_block", ["bytes"],
        ex.hex(), "offset_out_of_bounds")
    # 17. 嵌套动态：string[] 内唯一元素的偏移指向容器外
    noob = bytearray(oracle_encode(["string[]"], [["a"]]))
    # 布局：0:外偏移 32:个数1 64:元素偏移(=64) 96:len 128:'a'
    # 把元素偏移改成巨大越界值
    noob[64:96] = (0xFFFF).to_bytes(32, "big")
    add("nested_inner_offset_oob", ["string[]"],
        noob.hex(), "offset_out_of_bounds")
    return m


# --------------------------------------------------------------------------
# 选择器黄金值（手工钉死的知名值 + 本内核方法）
# --------------------------------------------------------------------------

KNOWN_SELECTORS = [
    ("transfer(address,uint256)", "a9059cbb", "ERC-20 知名选择器"),
    ("balanceOf(address)", "70a08231", "ERC-20 知名选择器"),
    ("approve(address,uint256)", "095ea7b3", "ERC-20 知名选择器"),
    ("totalSupply()", "18160ddd", "ERC-20 知名选择器"),
    ("allowance(address,address)", "dd62ed3e", "ERC-20 知名选择器"),
]


def keccak256(b: bytes) -> bytes:
    k = keccak.new(digest_bits=256)
    k.update(b)
    return k.digest()


def build_selectors():
    out = []
    for sig, hexv, src in KNOWN_SELECTORS:
        out.append({"signature": sig, "selector_hex": hexv, "source": src})
    # 内核方法由 pycryptodome 计算，eth_utils 交叉验证
    from eth_utils import keccak as eth_keccak
    for sig, _types in [
        ("mint(bytes20,uint256)", None),
        ("transfer(bytes20,bytes20,uint256)", None),
        ("setNote(bytes20,string)", None),
        ("noteOf(bytes20)", None),
    ]:
        a = keccak256(sig.encode())[:4].hex()
        b = eth_keccak(text=sig)[:4].hex()
        assert a == b, f"选择器实现不一致 {sig}"
        out.append({"signature": sig, "selector_hex": a,
                    "source": "pycryptodome x eth_utils"})
    return out


def main() -> None:
    import eth_abi

    payload = {
        "generated_by": f"eth_abi {eth_abi.__version__} (oracle) + hand-crafted malformed",
        "description": "受限 ABI 类型黄金向量：整数/字节串/数组/元组，含空串、负数、嵌套动态与恶意偏移",
        "valid": build_valid(),
        "malformed": build_malformed(),
        "selectors": build_selectors(),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"写入 {OUT}: {len(payload['valid'])} 有效, "
          f"{len(payload['malformed'])} 畸形, {len(payload['selectors'])} 选择器")


if __name__ == "__main__":
    main()
