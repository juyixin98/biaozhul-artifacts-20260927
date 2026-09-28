"""时间与信号内核：纯函数、整数有理运算，不做任何有损近似。

所有时基换算都以 ``out_ticks = round(src_ticks * s_num / s_den)`` 的
精确有理形式给出，并显式报告余数；规划器只在余数恒为零时接受直拼，
否则产生 TIMEBASE_INCOMPATIBLE 判定（要求转码，而不是伪装直拼）。

视频重排（B 帧导致的解码/呈现序差异）由 DTS/PTS 差值的保持来保证：
``out_pts - out_dts == scale(src_pts - src_dts)``。
"""
from __future__ import annotations

from fractions import Fraction
from typing import Iterable, Optional

import numpy as np

# ---------------------------------------------------------------- 有理工具


def gcd(a: int, b: int) -> int:
    return int(np.gcd(np.int64(abs(a)), np.int64(abs(b))))


def lcm(a: int, b: int) -> int:
    if a <= 0 or b <= 0:
        raise ValueError(f"lcm 要求正整数: {a}, {b}")
    a64, b64 = abs(int(a)), abs(int(b))
    g = int(np.gcd(np.int64(a64), np.int64(b64)))
    result = (a64 // g) * b64  # Python 任意精度，避免 int64 静默回绕
    if result > 0xFFFFFFFFFFFFFFFF:
        raise OverflowError(f"lcm 溢出: lcm({a},{b})={result}")
    return result


def lcm_many(values: Iterable[int]) -> int:
    acc = 1
    for v in values:
        acc = lcm(acc, int(v))
    return acc


def scale_factor(src_tb: tuple[int, int], out_tb: tuple[int, int]) -> tuple[int, int]:
    """从 src 时基到 out 时基的 ticks 缩放因子（未约分）。

    out_ticks = src_ticks * src_tb[0] * out_tb[1] / (src_tb[1] * out_tb[0])
    """
    sn, sd = src_tb
    on, od = out_tb
    return (sn * od, sd * on)


def reduce_factor(factor: tuple[int, int]) -> tuple[int, int]:
    num, den = factor
    g = gcd(num, den)
    return num // g, den // g


def convert_ticks(
    ticks: int,
    src_tb: tuple[int, int],
    out_tb: tuple[int, int],
) -> tuple[int, int]:
    """转换单个时间戳，返回 (转换后 ticks, 余数分子)。

    余数以源 ticks 的等价格表示：remainder 为 ``(src * s_num) % s_den``，
    调用方据此判断换算是否无损。
    """
    s_num, s_den = scale_factor(src_tb, out_tb)
    val = ticks * s_num
    return val // s_den, val % s_den


def convert_series(
    ticks: np.ndarray,
    src_tb: tuple[int, int],
    out_tb: tuple[int, int],
) -> tuple[np.ndarray, np.ndarray]:
    """向量化转换：返回 (out_ticks:int64[], remainders:int64[])。

    数值可能超 int64 时自动回退到 Python 任意精度整数，避免静默回绕；
    回退结果再转回 int64（真正超界时由上层溢出检查捕获）。
    """
    s_num, s_den = scale_factor(src_tb, out_tb)
    t = ticks.astype(np.int64)
    if t.size and int(np.max(np.abs(t))) * abs(s_num) > 0x7FFFFFFFFFFFFFFF:
        val = np.array([int(x) * s_num for x in t], dtype=object)
        out = np.array([v // s_den for v in val], dtype=np.int64)
        rem = np.array([v % s_den for v in val], dtype=np.int64)
        return out, rem
    val = t * np.int64(s_num)
    return np.floor_divide(val, np.int64(s_den)), np.mod(val, np.int64(s_den))


def conversion_is_lossless(
    src_tb: tuple[int, int],
    out_tb: tuple[int, int],
) -> bool:
    """缩放因子约分后分母为 1 ⇒ 任何整数时间戳均可无损转换。"""
    num, den = reduce_factor(scale_factor(src_tb, out_tb))
    return den == 1


def seconds_to_ticks(seconds: float, time_base: tuple[int, int]) -> int:
    n, d = time_base
    return int(round(seconds * d / n))


def seconds_to_ticks_exact(seconds: float, time_base: tuple[int, int]) -> tuple[int, bool]:
    """精确（有理数）换算：返回 (ticks, 是否恰好落在 tick 网格)。"""
    n, d = time_base
    frac = Fraction(str(seconds)) * d / n
    return int(frac), frac.denominator == 1


def audio_frame_is_integral(
    frame_pcm: int, sample_rate: int, out_tb: tuple[int, int]
) -> tuple[int, int]:
    """AAC 帧长在输出时基下是否为整数 ticks；返回 (ticks, 余数)。"""
    num = frame_pcm * out_tb[1]
    den = sample_rate * out_tb[0]
    g = gcd(num, den)
    num //= g
    den //= g
    return num // den, num % den


def ticks_to_seconds(ticks: int, time_base: tuple[int, int]) -> float:
    n, d = time_base
    return ticks * n / d


# ------------------------------------------------------------ 输出时基选择


class TimebaseSelectionError(ValueError):
    def __init__(self, reason: str, evidence: dict):
        super().__init__(reason)
        self.reason = reason
        self.evidence = evidence


def choose_output_timebase(
    source_time_bases: list[tuple[int, int]],
    container: str,
    mpegts_clock: int = 90000,
    max_timescale: int = 0xFFFFFFFF,
) -> tuple[int, int]:
    """依据容器约束选择统一输出时基。

    - ``mpegts``：时钟固定（视频/音频均为 1/90000），源时基必须能整除它。
    - ``mp4``/``fmp4``：时标取各源分母的最小公倍数，必须可放入 uint32。

    源时基 num 必须为 1（标准容器中 ticks 都是 1/den 秒）。
    """
    if not source_time_bases:
        raise TimebaseSelectionError("no_streams", {"source_time_bases": []})

    nums = {tb[0] for tb in source_time_bases}
    if nums != {1}:
        raise TimebaseSelectionError(
            "non_unit_timebase_num",
            {"source_time_bases": source_time_bases},
        )

    dens = [tb[1] for tb in source_time_bases]
    c = container.lower()
    if c in {"mpegts", "ts", "m2ts"}:
        bad = [d for d in dens if mpegts_clock % d != 0]
        if bad:
            raise TimebaseSelectionError(
                "mpegts_clock_not_divisible",
                {"clock": mpegts_clock, "bad_source_denominators": bad},
            )
        return (1, mpegts_clock)

    if c in {"mp4", "fmp4", "mov", "mkv"}:
        try:
            den = lcm_many(dens)
        except OverflowError:
            raise TimebaseSelectionError(
                "timescale_overflow",
                {"lcm_timescale": None, "max_timescale": int(max_timescale),
                 "source_denominators": dens},
            )
        if den > max_timescale:
            raise TimebaseSelectionError(
                "timescale_overflow",
                {"lcm_timescale": int(den), "max_timescale": int(max_timescale)},
            )
        return (1, int(den))

    raise TimebaseSelectionError("unknown_container", {"container": container})


# ------------------------------------------------------ DTS/PTS 重定位内核


INT64_MAX = 0x7FFFFFFFFFFFFFFF


def scaled_ticks_fit_int64(ticks_max_abs: int,
                           src_tb: tuple[int, int],
                           out_tb: tuple[int, int]) -> bool:
    """转换后的最大绝对值是否可放入 int64（容器 DTS/PTS 字段宽度）。"""
    s_num, s_den = scale_factor(src_tb, out_tb)
    return abs(int(ticks_max_abs)) * abs(s_num) // max(s_den, 1) <= INT64_MAX


def rebase_series(
    src_dts: np.ndarray,
    src_pts: np.ndarray,
    src_durations: np.ndarray,
    src_tb: tuple[int, int],
    out_tb: tuple[int, int],
    out_origin: int,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """把一组样本平移到输出时间轴。

    ``out_origin`` 是源首样本 DTS（通常为 0）映射到的输出 tick 值。
    返回 (out_dts, out_pts, out_duration, remainders)。

    保持 DTS↔PTS 的偏移（重排信息），并对有损换算给出逐样本余数。
    """
    out_dts, rem_dts = convert_series(src_dts, src_tb, out_tb)
    out_pts, rem_pts = convert_series(src_pts, src_tb, out_tb)
    out_dur, rem_dur = convert_series(src_durations, src_tb, out_tb)
    out_dts += np.int64(out_origin)
    out_pts += np.int64(out_origin)
    remainders = np.maximum.reduce([rem_dts, rem_pts, rem_dur])
    return out_dts, out_pts, out_dur, remainders


def assert_monotonic_nonnegative(dts: np.ndarray) -> Optional[int]:
    """返回首个违规样本 index：负 DTS、回退或**重复** DTS；无违规则 None。

    MP4/MPEG-TS 均不允许同流重复 DTS。
    """
    if dts.size == 0:
        return None
    if int(dts[0]) < 0:
        return 0
    diffs = np.diff(dts.astype(np.int64))
    bad = np.nonzero(diffs <= 0)[0]
    if bad.size:
        return int(bad[0]) + 1
    return None


# --------------------------------------------------------- 音频 priming 数学


def priming_drop(
    encoder_delay_samples: int,
    packet_pcm: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """AAC 等编码器延迟（priming）扣减。

    返回 (每包贡献 PCM[], 角色标记 0=content/1=drop_encoder_delay)。
    priming 样本依次从首包起扣除：被完全吃掉的包整包标记为 drop，
    落在边界上的包扣除差额，其余为完整贡献。

    例：delay=2112, packet=1024 ⇒ [0(全弃), 1024(全弃), 960, 1024, ...]
    """
    contributions = packet_pcm.astype(np.int64).copy()
    roles = np.zeros(packet_pcm.shape, dtype=np.int64)
    remaining = int(encoder_delay_samples)
    for i in range(contributions.shape[0]):
        if remaining <= 0:
            break
        take = min(remaining, int(contributions[i]))
        contributions[i] -= take
        remaining -= take
        roles[i] = 1  # 该包含需要丢弃的 priming
    return contributions, roles


def tail_padding_count(
    video_end_ticks: Optional[int],
    audio_end_ticks: Optional[int],
    audio_frame_ticks: int,
) -> int:
    """音频短于视频时，需要补足的整包数（向上取整）。"""
    if video_end_ticks is None or audio_end_ticks is None:
        return 0
    gap = video_end_ticks - audio_end_ticks
    if gap <= 0:
        return 0
    return int(np.ceil(gap / audio_frame_ticks))


def reference_closure(
    retained: set[int],
    references: dict[int, list[int]],
    stop_at: Optional[set[int]] = None,
) -> set[int]:
    """解码参考闭包：从保留集合出发递归纳入被引用帧。

    ``stop_at`` 中的帧被纳入后不再向其引用继续展开——用于在**真正的
    解码器重启点**（无外部引用的 IDR / recovery point）处终止回溯。
    注意：仅凭 keyframe 标志不应作为停止条件，开放 GOP 的关键帧可能仍
    引用更早帧；是否可停由调用方依据该关键帧是否携带外部引用决定。
    """
    stop_at = stop_at or set()
    out: set[int] = set()
    stack = list(retained)
    while stack:
        idx = stack.pop()
        if idx in out:
            continue
        out.add(idx)
        if idx in stop_at:
            # 解码器重启点：纳入但不继续展开其引用
            continue
        for ref in references.get(idx, ()):  # type: ignore[arg-type]
            if ref not in out:
                stack.append(ref)
    return out


def fraction_str(tb: tuple[int, int]) -> str:
    return f"{tb[0]}/{tb[1]}"
