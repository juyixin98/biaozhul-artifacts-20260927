"""Configuration with explicit defaults and environment overrides.

All delay bounds and clock parameters are defined here (never buried in
demonstration code) so the adaptive-delay contract is auditable in one place.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return float(raw)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


@dataclass(frozen=True)
class JitterConfig:
    # --- RTP clock ---
    clock_rate: int = 8000            # RTP timestamp ticks per second
    samples_per_packet: int = 80      # constant payload (10 ms @ 8 kHz)
    seq_bits: int = 16                # RTP sequence-number width
    ts_bits: int = 32                 # RTP timestamp width

    # --- Adaptive delay bounds (milliseconds), the contract from the spec ---
    min_delay_ms: float = 20.0        # hard floor
    max_delay_ms: float = 120.0       # hard ceiling
    safety_margin_ms: float = 2.0     # additive margin d_v
    jitter_multiplier: float = 8.0    # K in d = q + K*j + d_v

    # --- Buffer capacity (bounded queue) ---
    max_buffer_packets: int = 256

    # A missing head is confirmed as a real hole once the highest seq seen is
    # this many packets ahead of it. A value >= 2 means ordinary adjacent
    # reordering (n arriving just after n+1) is still rescued, but a genuine
    # burst loss or a very late packet is declared at its deadline.
    late_packet_threshold: int = 2

    # --- Fixed baseline used for comparison ---
    fixed_delay_ms: float = 20.0

    @property
    def frame_ms(self) -> float:
        return 1000.0 * self.samples_per_packet / self.clock_rate

    @classmethod
    def from_env(cls) -> "JitterConfig":
        return cls(
            clock_rate=_env_int("RTP_CLOCK_RATE", cls.clock_rate),
            samples_per_packet=_env_int("RTP_SAMPLES_PER_PACKET", cls.samples_per_packet),
            min_delay_ms=_env_float("JB_MIN_DELAY_MS", cls.min_delay_ms),
            max_delay_ms=_env_float("JB_MAX_DELAY_MS", cls.max_delay_ms),
            safety_margin_ms=_env_float("JB_SAFETY_MARGIN_MS", cls.safety_margin_ms),
            jitter_multiplier=_env_float("JB_JITTER_K", cls.jitter_multiplier),
            max_buffer_packets=_env_int("JB_MAX_PACKETS", cls.max_buffer_packets),
            fixed_delay_ms=_env_float("JB_FIXED_DELAY_MS", cls.fixed_delay_ms),
        )
