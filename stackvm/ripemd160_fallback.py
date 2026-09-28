"""纯 Python RIPEMD-160 回退实现（无第三方依赖）。

当 OpenSSL 未提供 hashlib.new("ripemd160")（部分发行版默认裁剪遗留算法）时使用。
参数表来自 RIPEMD-160 规范；正确性由 tests/test_hashes_vectors.py 以 hashlib
（OpenSSL）在任意输入上逐字节交叉校验，而不是仅靠自测向量。
"""
from __future__ import annotations

import struct

_MASK = 0xFFFFFFFF

# 左线：轮内消息字选择与循环左移位数
_R = [
    0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15,
    7, 4, 13, 1, 10, 6, 15, 3, 12, 0, 9, 5, 2, 14, 11, 8,
    3, 10, 14, 4, 9, 15, 8, 1, 2, 7, 0, 6, 13, 11, 5, 12,
    1, 9, 11, 10, 0, 8, 12, 4, 13, 3, 7, 15, 14, 5, 6, 2,
    4, 0, 5, 9, 7, 12, 2, 10, 14, 1, 3, 8, 11, 6, 15, 13,
]
_S = [
    11, 14, 15, 12, 5, 8, 7, 9, 11, 13, 14, 15, 6, 7, 9, 8,
    7, 6, 8, 13, 11, 9, 7, 15, 7, 12, 15, 9, 11, 7, 13, 12,
    11, 13, 6, 7, 14, 9, 13, 15, 14, 8, 13, 6, 5, 12, 7, 5,
    11, 12, 14, 15, 14, 15, 9, 8, 9, 14, 5, 6, 8, 6, 5, 12,
    9, 15, 5, 11, 6, 8, 13, 12, 5, 12, 13, 14, 11, 8, 5, 6,
]
# 右线
_RP = [
    5, 14, 7, 0, 9, 2, 11, 4, 13, 6, 15, 8, 1, 10, 3, 12,
    6, 11, 3, 7, 0, 13, 5, 10, 14, 15, 8, 12, 4, 9, 1, 2,
    15, 5, 1, 3, 7, 14, 6, 9, 11, 8, 12, 2, 10, 0, 4, 13,
    8, 6, 4, 1, 3, 11, 15, 0, 5, 12, 2, 13, 9, 7, 10, 14,
    12, 15, 10, 4, 1, 5, 8, 7, 6, 2, 13, 14, 0, 3, 9, 11,
]
_SP = [
    8, 9, 9, 11, 13, 15, 15, 5, 7, 7, 8, 11, 14, 14, 12, 6,
    9, 13, 15, 7, 12, 8, 9, 11, 7, 7, 12, 7, 6, 15, 13, 11,
    9, 7, 15, 11, 8, 6, 6, 14, 12, 13, 5, 14, 13, 13, 7, 5,
    15, 5, 8, 11, 14, 14, 6, 14, 6, 9, 12, 9, 12, 5, 15, 8,
    8, 5, 12, 9, 12, 5, 14, 6, 8, 13, 6, 5, 15, 13, 11, 11,
]


def _rol(x: int, n: int) -> int:
    return ((x << n) | (x >> (32 - n))) & _MASK


def _f(j: int, x: int, y: int, z: int) -> int:
    if j < 16:
        return x ^ y ^ z
    if j < 32:
        return (x & y) | ((~x & _MASK) & z)
    if j < 48:
        return (x | (~y & _MASK)) ^ z
    if j < 64:
        return (x & z) | (y & (~z & _MASK))
    return x ^ (y | (~z & _MASK))


def _fp(j: int, x: int, y: int, z: int) -> int:
    # 右线轮函数顺序相反
    if j < 16:
        return x ^ (y | (~z & _MASK))
    if j < 32:
        return (x & z) | (y & (~z & _MASK))
    if j < 48:
        return (x | (~y & _MASK)) ^ z
    if j < 64:
        return (x & y) | ((~x & _MASK) & z)
    return x ^ y ^ z


def _k(j: int) -> int:
    if j < 16:
        return 0x00000000
    if j < 32:
        return 0x5A827999
    if j < 48:
        return 0x6ED9EBA1
    if j < 64:
        return 0x8F1BBCDC
    return 0xA953FD4E


def _kp(j: int) -> int:
    if j < 16:
        return 0x50A28BE6
    if j < 32:
        return 0x5C4DD124
    if j < 48:
        return 0x6D703EF3
    if j < 64:
        return 0x7A6D76E9
    return 0x00000000


def ripemd160(message: bytes) -> bytes:
    message = bytes(message)
    bit_len = (len(message) * 8) & 0xFFFFFFFFFFFFFFFF
    padded = bytearray(message)
    padded.append(0x80)
    while len(padded) % 64 != 56:
        padded.append(0)
    padded += struct.pack("<Q", bit_len)

    h0 = 0x67452301
    h1 = 0xEFCDAB89
    h2 = 0x98BADCFE
    h3 = 0x10325476
    h4 = 0xC3D2E1F0

    for block in (padded[i:i + 64] for i in range(0, len(padded), 64)):
        x = list(struct.unpack("<16I", block))
        al, bl, cl, dl, el = h0, h1, h2, h3, h4
        ar, br, cr, dr, er = h0, h1, h2, h3, h4
        for j in range(80):
            t = (_rol((al + _f(j, bl, cl, dl) + x[_R[j]] + _k(j)) & _MASK, _S[j]) + el) & _MASK
            al, el, dl, cl, bl = el, dl, _rol(cl, 10), bl, t
            t = (_rol((ar + _fp(j, br, cr, dr) + x[_RP[j]] + _kp(j)) & _MASK, _SP[j]) + er) & _MASK
            ar, er, dr, cr, br = er, dr, _rol(cr, 10), br, t

        t = (h1 + cl + dr) & _MASK
        h1 = (h2 + dl + er) & _MASK
        h2 = (h3 + el + ar) & _MASK
        h3 = (h4 + al + br) & _MASK
        h4 = (h0 + bl + cr) & _MASK
        h0 = t

    return struct.pack("<5I", h0, h1, h2, h3, h4)
