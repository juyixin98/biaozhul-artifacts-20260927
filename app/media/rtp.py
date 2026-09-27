"""RTP 报文解析与构造（RFC 3550 第 5 章）。

本模块只做线格式解析，不做任何网络 IO。解析失败抛 :class:`RtpParseError`，
错误带机器可读的 ``reason``，供接口层区分失败类别。
"""

from __future__ import annotations

from dataclasses import dataclass

RTP_VERSION = 2
HEADER_FIXED_LEN = 12
CSRC_LEN = 4
EXT_HEADER_LEN = 4
WORD = 4


class RtpParseError(ValueError):
    """RTP 报文不合规。``reason`` 为稳定的机器可读错误码。"""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail

    def to_dict(self) -> dict:
        return {"reason": self.reason, "detail": self.detail}


@dataclass(frozen=True)
class RtpPacket:
    version: int
    padding: bool
    extension: bool
    csrc_count: int
    marker: bool
    payload_type: int
    sequence: int  # 16 位线值，0..65535
    timestamp: int  # 32 位线值，0..2^32-1
    ssrc: int
    csrc: tuple[int, ...]
    payload: bytes
    raw: bytes

    @property
    def size(self) -> int:
        return len(self.raw)


def parse_rtp(data: bytes | bytearray | memoryview) -> RtpPacket:
    """严格解析一个 RTP 报文。

    失败类别（``RtpParseError.reason``）：

    - ``truncated``        长度不足固定头/CSRC/扩展头
    - ``bad_version``      版本号不是 2
    - ``bad_payload_type`` PT 保留（>=128）
    - ``bad_extension``    扩展声明的长度越界
    - ``bad_padding``      填充声明越界或填充计数为 0
    """
    try:
        buf = bytes(data)
    except TypeError as exc:
        raise RtpParseError("truncated", f"非字节输入: {exc!r}") from exc

    if len(buf) < HEADER_FIXED_LEN:
        raise RtpParseError("truncated", f"报文 {len(buf)} 字节，少于固定头 12 字节")

    b0 = buf[0]
    version = (b0 >> 6) & 0x3
    padding_flag = bool(b0 & 0x20)
    extension_flag = bool(b0 & 0x10)
    csrc_count = b0 & 0x0F

    b1 = buf[1]
    marker = bool(b1 & 0x80)
    payload_type = b1 & 0x7F  # 7 位字段，线值域恒为 0..127

    if version != RTP_VERSION:
        raise RtpParseError("bad_version", f"版本号={version}，仅支持 RTPv2")

    sequence = (buf[2] << 8) | buf[3]
    timestamp = (buf[4] << 24) | (buf[5] << 16) | (buf[6] << 8) | buf[7]
    ssrc = (buf[8] << 24) | (buf[9] << 16) | (buf[10] << 8) | buf[11]

    offset = HEADER_FIXED_LEN
    csrc_end = offset + csrc_count * CSRC_LEN
    if csrc_end > len(buf):
        raise RtpParseError("truncated", "CSRC 列表超出报文长度")
    csrc = tuple(
        int.from_bytes(buf[offset + i * 4 : offset + (i + 1) * 4], "big")
        for i in range(csrc_count)
    )
    offset = csrc_end

    if extension_flag:
        if offset + EXT_HEADER_LEN > len(buf):
            raise RtpParseError("bad_extension", "扩展头超出报文长度")
        ext_words = int.from_bytes(buf[offset + 2 : offset + 4], "big")
        ext_end = offset + EXT_HEADER_LEN + ext_words * WORD
        if ext_end > len(buf):
            raise RtpParseError("bad_extension", f"扩展数据 {ext_end} 超出报文 {len(buf)}")
        offset = ext_end

    payload_end = len(buf)
    if padding_flag:
        pad_count = buf[-1]
        if pad_count <= 0 or offset + pad_count > len(buf):
            raise RtpParseError("bad_padding", f"填充计数 {pad_count} 超出可用负载区")
        payload_end = len(buf) - pad_count
        if payload_end < offset:
            raise RtpParseError("bad_padding", "填充覆盖了头部")

    payload = buf[offset:payload_end]
    return RtpPacket(
        version=RTP_VERSION,
        padding=padding_flag,
        extension=extension_flag,
        csrc_count=csrc_count,
        marker=marker,
        payload_type=payload_type,
        sequence=sequence,
        timestamp=timestamp,
        ssrc=ssrc,
        csrc=csrc,
        payload=payload,
        raw=buf,
    )


def build_rtp(
    *,
    sequence: int,
    timestamp: int,
    ssrc: int,
    payload: bytes,
    marker: bool = False,
    payload_type: int = 0,
    csrc: tuple[int, ...] = (),
    padding: int = 0,
) -> bytes:
    """构造一个 RTP 报文（测试/夹具用）。

    ``sequence``/``timestamp`` 传线值（取模 2**16 / 2**32）。
    ``padding>0`` 时追加该字节数的填充并置 P 位，末字节写入填充计数。
    """
    if not 0 <= payload_type < 128:
        raise ValueError("payload_type 必须在 0..127")
    if not 0 <= len(csrc) <= 15:
        raise ValueError("CSRC 数量 0..15")
    sequence %= 1 << 16
    timestamp %= 1 << 32
    ssrc %= 1 << 32

    b0 = (RTP_VERSION << 6) | (1 if padding else 0) << 5 | len(csrc)
    b1 = (1 if marker else 0) << 7 | payload_type
    out = bytearray(
        bytes([b0, b1])
        + sequence.to_bytes(2, "big")
        + timestamp.to_bytes(4, "big")
        + ssrc.to_bytes(4, "big")
    )
    for c in csrc:
        out += int(c % (1 << 32)).to_bytes(4, "big")
    out += payload
    if padding:
        out += b"\x00" * (padding - 1) + bytes([padding])
    return bytes(out)
