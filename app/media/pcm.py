"""合成 PCM 负载：本地确定性正弦 + 包内容校验。

不读取任何外部音频。每个包负载为 16-bit 小端单声道 PCM，
样本由 ``(ssrc, 序号全展开值, 时间戳全展开值)`` 决定，因此：

- 序号/时间戳展开正确时可逐样本回放校验；
- 缺包留下的空缺无法由相邻包“猜”出，空缺必须显式标记。
"""

from __future__ import annotations

import numpy as np

# 固定基准相位，保证夹具可复现（不依赖随机全局状态）
_PHASE_ZERO = 0.0


def sine_frame(
    *,
    ssrc: int,
    seq_ext: int,
    ts_ext: int,
    samples: int,
    clock_rate: int = 8000,
    freq_hz: float = 440.0,
    amplitude: float = 0.25,
) -> bytes:
    """生成一个 RTP 包对应的 PCM 帧。

    相位严格由发送端采样时刻 ``ts_ext`` 决定（时钟漂移时 ts 步进本身会变，
    因此相位连续反映的是发送时钟）。
    """
    t = (np.arange(samples, dtype=np.float64) + ts_ext) / clock_rate
    phase = 2.0 * np.pi * freq_hz * t + _PHASE_ZERO
    wave = amplitude * np.sin(phase)
    # 注入一个极弱的、由 ssrc/序号决定的签名分量，便于内容归属校验
    sign = 1e-3 * (((ssrc ^ (seq_ext * 0x9E3779B1)) & 0xFF) / 255.0 - 0.5)
    pcm = np.round(np.clip(wave + sign, -1.0, 1.0) * 32767).astype("<i2")
    return pcm.tobytes()


def decode_pcm(payload: bytes) -> np.ndarray:
    """把 PCM 负载解码为 float64 数组（-1..1）。奇数尾字节视为协议错误。"""
    if len(payload) % 2 != 0:
        raise ValueError("PCM16 负载长度必须为偶数")
    return np.frombuffer(payload, dtype="<i2").astype(np.float64) / 32768.0


def payload_matches(
    payload: bytes,
    *,
    ssrc: int,
    seq_ext: int,
    ts_ext: int,
    samples: int,
    clock_rate: int = 8000,
    freq_hz: float = 440.0,
) -> bool:
    """逐字节校验收到的负载与期望合成内容是否一致。"""
    expected = sine_frame(
        ssrc=ssrc,
        seq_ext=seq_ext,
        ts_ext=ts_ext,
        samples=samples,
        clock_rate=clock_rate,
        freq_hz=freq_hz,
    )
    return payload == expected
