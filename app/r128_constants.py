"""EBU R128 / ITU-R BS.1770-4 measurement constants.

Every magic number in the loudness kernel is defined here, with the clause that
mandates it. Nothing in the kernel hard-codes an undocumented threshold.

References
----------
- ITU-R BS.1770-4 (2015), "Algorithms to measure audio programme loudness and
  true-peak audio level": K-weighting, channel weights, gating blocks.
- EBU R 128 v4 (2020): integrated loudness, gating.
- EBU Tech 3342-2016: loudness range (LRA): 3 s blocks, <=2/3 overlap,
  -20 LU relative gate, 0.1 LU histogram, 10th/95th percentiles.
"""
from __future__ import annotations

KERNEL_VERSION = "1.0.0"
SPEC_ID = "ITU-R BS.1770-4 + EBU R128 v4 + EBU Tech 3342 (2016)"

# ---------------------------------------------------------------------------
# Timing (BS.1770-4 §6 / EBU Tech 3342 §6)
# ---------------------------------------------------------------------------
SAMPLE_RATE_HZ = 48_000          # the only normative rate; other rates rejected

HOP_SECONDS = 0.100              # analysis hop: one "frame" (10 Hz rate)
INTEGRATED_BLOCK_SECONDS = 0.400 # gating block T_g for integrated loudness
LRA_BLOCK_SECONDS = 3.0          # short-term block for LRA
# EBU Tech 3342 §6.2: 3 s blocks whose overlap "shall not exceed 2/3".
# ffmpeg/libebur128 records them on a 1 s cadence (counter resets to 20 after
# hitting 30 frames), i.e. 3 s window / 1 s hop -> 2/3 overlap.
LRA_HOP_SECONDS = 1.0

HOP_SAMPLES = int(round(SAMPLE_RATE_HZ * HOP_SECONDS))                 # 4800
INTEGRATED_BLOCK_FRAMES = int(round(INTEGRATED_BLOCK_SECONDS / HOP_SECONDS))  # 4
LRA_BLOCK_FRAMES = int(round(LRA_BLOCK_SECONDS / HOP_SECONDS))         # 30
LRA_HOP_FRAMES = int(round(LRA_HOP_SECONDS / HOP_SECONDS))             # 10

# ---------------------------------------------------------------------------
# Gating (BS.1770-4 §6; EBU R128 §6)
# ---------------------------------------------------------------------------
ABSOLUTE_GATE_LUFS = -70.0       # Gamma_a: absolute gate (Table 3)
RELATIVE_GATE_OFFSET_LU = -10.0  # Gamma_r offset for integrated loudness
LRA_RELATIVE_GATE_OFFSET_LU = -20.0  # EBU Tech 3342: mean gated STL minus 20
LOUDNESS_OFFSET_DB = -0.691      # -0.691 dB (Table 2), rounded from 10log10 K

# ---------------------------------------------------------------------------
# Channel weights (BS.1770-4 Table 2), applied to mean-square energy.
# ---------------------------------------------------------------------------
CHANNEL_WEIGHT_NORMAL = 1.0
CHANNEL_WEIGHT_SURROUND = 1.41   # +1.5 dB nominal; ffmpeg/libebur128 use 1.41
# Note: 10*log10(1.41) = 1.492 dB. The two-decimal 1.41 factor is the value
# shared by ffmpeg, libebur128 and pyloudnorm, so we use it for cross-tool
# agreement rather than the analytic 10**(1.5/10)=1.41254.
CHANNEL_WEIGHT_DUAL_MONO = 2.0   # +3.01 dB (DualMono flag, BS.1770 Table 2 note)

# Canonical K-weighting biquad coefficients at 48 kHz, in transposed
# direct-form-II with a0 == 1. Stage 1 = high-shelf (pre-filter),
# stage 2 = RLB high-pass. These are the published BS.1770-4 values used
# verbatim by ffmpeg (libavfilter/ebur128.c, ebur128 coefficients block) and
# reproduced to ~1e-7 by the "DeMan" analogue-prototype design in pyloudnorm.
# Verified in this project against pyloudnorm 0.2.0's DeMan design and against
# ffmpeg's end-to-end ebur128 measurements.
PRE_FILTER_B = (1.53512485958697, -2.69169618940638, 1.19839281085285)
PRE_FILTER_A = (1.0, -1.69065929318241, 0.73248077421585)
RLB_B = (1.0, -2.0, 1.0)
RLB_A = (1.0, -1.99004745483398, 0.99007225036621)

# ---------------------------------------------------------------------------
# LRA histogram (EBU Tech 3342 §6.2; ffmpeg/libebur128 implementation)
# ---------------------------------------------------------------------------
LRA_BIN_LU = 0.1
# Bin i spans loudness [-70 + 0.1 i, -70 + 0.1(i+1)); representative at
# -69.95 + 0.1 i. Percentiles use the ffmpeg ranks with no within-bin
# interpolation:
#   p_lo = floor((N-1)*0.10 + 0.5)
#   p_hi = floor((N-1)*0.95 + 0.5)
LRA_PERCENTILE_LOW = 0.10
LRA_PERCENTILE_HIGH = 0.95

# ---------------------------------------------------------------------------
# Channel roles and default layouts
# ---------------------------------------------------------------------------
# Roles carry their BS.1770 weight. LFE is not weighted (excluded from sum).
ROLE_L = "L"
ROLE_R = "R"
ROLE_C = "C"
ROLE_LFE = "LFE"
ROLE_Ls = "Ls"
ROLE_Rs = "Rs"
ROLE_Lb = "Lb"
ROLE_Rb = "Rb"
ROLE_DUAL_MONO = "DualMono"

ROLE_WEIGHTS = {
    ROLE_L: CHANNEL_WEIGHT_NORMAL,
    ROLE_R: CHANNEL_WEIGHT_NORMAL,
    ROLE_C: CHANNEL_WEIGHT_NORMAL,
    ROLE_Ls: CHANNEL_WEIGHT_SURROUND,
    ROLE_Rs: CHANNEL_WEIGHT_SURROUND,
    ROLE_Lb: CHANNEL_WEIGHT_SURROUND,
    ROLE_Rb: CHANNEL_WEIGHT_SURROUND,
    ROLE_DUAL_MONO: CHANNEL_WEIGHT_DUAL_MONO,
    # LFE deliberately absent: it contributes zero to the loudness sum.
}

# Defaults for the channel counts we accept. 5.1 uses the ITU/SMPTE WAVE order
# which matches ffmpeg's guessed 5.1 layout: L R C LFE Ls Rs. A plain mono file
# is weighted 1.0 like ffmpeg (mapped to M/front-left); the +3.01 dB DualMono
# role is only used when a client explicitly declares a dual-mono channel.
DEFAULT_LAYOUTS = {
    1: [ROLE_L],
    2: [ROLE_L, ROLE_R],
    6: [ROLE_L, ROLE_R, ROLE_C, ROLE_LFE, ROLE_Ls, ROLE_Rs],
}

# ---------------------------------------------------------------------------
# Measurement statuses (returned as stable strings in the API)
# ---------------------------------------------------------------------------
STATUS_OK = "OK"
STATUS_SILENCE = "SILENCE"                       # digital silence, no signal
STATUS_NOT_COMPUTED = "NOT_COMPUTED"             # blocks exist, none above gate
STATUS_INSUFFICIENT_BLOCKS = "INSUFFICIENT_BLOCKS"  # fewer blocks than needed
STATUS_ERROR = "ERROR"

# True-peak position: this kernel never oversamples and must never claim TP.
TRUE_PEAK_NOT_MEASURED = "NOT_MEASURED"
