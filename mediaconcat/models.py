"""核心数据模型：对真实容器元数据的结构化描述。

这些类型刻意与 ffmpeg/ffprobe 无关：``StreamProbe`` 是 ffprobe
流信息的最小公共子集，合成夹具直接产出同样的结构，从而保证
测试与真实输入走同一条规划路径。
"""
from __future__ import annotations

from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

MediaType = Literal["video", "audio"]


class SampleUnit(str, Enum):
    """音频样本标记的单位。"""

    PCM_SAMPLES = "pcm_samples"
    ENCODER_DELAY = "encoder_delay_samples"  # 编码器延迟（priming）样本数


class FrameRecord(BaseModel):
    """逐样本（逐帧/逐音频包）记录 —— 规划可逐样本检验的基础。

    时间戳以各自流的时基为单位（整数 ticks）。视频帧额外携带
    关键帧标志与（开放 GOP 下）解码所需参考帧信息。
    """

    index: int = Field(..., ge=0, description="在源流中的包序号（解码顺序）")
    dts: int = Field(..., description="解码时间戳，流时基 ticks")
    pts: int = Field(..., description="呈现时间戳，流时基 ticks")
    duration: int = Field(..., gt=0, description="样本持续，流时基 ticks")
    keyframe: bool = Field(False, description="视频：是否为关键帧；音频恒为 True")
    # 开放 GOP：该帧解码所依赖的、可能位于剪切点之前的参考帧 index 列表
    references: list[int] = Field(default_factory=list)
    # 音频：该包覆盖的 PCM 样本数（解码输出，含延迟填充）
    pcm_samples: Optional[int] = None


class StreamProbe(BaseModel):
    """单条流的解析结果（ffprobe 流信息的最小子集）。"""

    codec: str
    codec_type: MediaType
    time_base: tuple[int, int] = Field(..., description="流时基，如 (1, 90000)")
    start_time_ticks: int = Field(0, description="首包 DTS（流时基 ticks），可为 0")
    frames: list[FrameRecord]
    width: Optional[int] = None
    height: Optional[int] = None
    # stream-copy 还要求这些编码参数一致；规则为“已知取值不统一即拒绝”。
    # 合成 sidecar 显式提供这些字段；ffprobe 适配器也尽量填充真实值，
    # 并用 extradata 哈希承载 SPS/PPS 一致性。逐帧 GOP 闭合性由
    # reference_info_complete 单独表达（ffprobe 无法确认时保守拒绝边界直拼）。
    pixel_format: Optional[str] = None
    profile: Optional[str] = None
    level: Optional[int] = None
    sample_aspect_ratio: Optional[str] = None
    # SPS/PPS/AudioSpecificConfig 等 extradata 的一致性标识（如哈希）
    extradata_id: Optional[str] = None
    # 逐帧 references 是否完整可信。合成 sidecar 显式给出（True）；
    # ffprobe 单包字段无法恢复开放 GOP 引用时为 False——此时多片段拼接边界
    # 无法确认闭合 GOP，规划器保守拒绝直拼。
    reference_info_complete: bool = True
    sample_rate: Optional[int] = None
    channels: Optional[int] = None
    audio_object_type: Optional[str] = None  # 如 AAC-LC / HE-AAC
    # 编码器延迟：AAC 等格式的 priming samples（如 2112）
    encoder_delay_samples: int = Field(0, ge=0)
    # 容器/编码对时基的限制：MPEG-TS 要求 1/90000，MP4 为 1/timescale
    container: str = "mp4"

    @field_validator("time_base")
    @classmethod
    def _tb_positive(cls, v: tuple[int, int]) -> tuple[int, int]:
        if len(v) != 2 or v[0] <= 0 or v[1] <= 0:
            raise ValueError(f"time_base 必须为 (num,den) 且均为正: {v}")
        return v

    @property
    def duration_seconds(self) -> float:
        if not self.frames:
            return 0.0
        # B 帧重排后解码序末帧未必是呈现序最后一帧
        last_pts_end = max(f.pts + f.duration for f in self.frames)
        return (last_pts_end - self.frames[0].pts) * self.time_base[0] / self.time_base[1]


class ClipProbe(BaseModel):
    """一个输入片段的解析结果：一个视频流 + 至多一个音频流。"""

    source: str = Field(..., description="可读的输入标识（文件路径或夹具名）")
    video: Optional[StreamProbe] = None
    audio: Optional[StreamProbe] = None
    # 请求的裁剪入点/出点（秒，相对片段起点）。None 表示不裁剪。
    cut_in_sec: Optional[float] = None
    cut_out_sec: Optional[float] = None

    def streams(self) -> list[StreamProbe]:
        return [s for s in (self.video, self.audio) if s is not None]


class FailureCode(str, Enum):
    """规划失败类别 —— 异常/未知绝不统一返回成功。"""

    CODEC_MISMATCH = "codec_mismatch"
    TIMEBASE_INCOMPATIBLE = "timebase_incompatible"
    VIDEO_PARAM_MISMATCH = "video_param_mismatch"
    AUDIO_PARAM_MISMATCH = "audio_param_mismatch"
    NON_KEYFRAME_CUT = "non_keyframe_cut"
    OPEN_GOP_REFERENCE_LOST = "open_gop_reference_lost"
    NEGATIVE_DTS = "negative_dts"
    TIMEBASE_CONVERSION_OVERFLOW = "timebase_conversion_overflow"
    EMPTY_INPUT = "empty_input"
    INPUT_NOT_FOUND = "input_not_found"
    GOP_STRUCTURE_UNKNOWN = "gop_structure_unknown"
    INVALID_RANGE = "invalid_range"
    CONTAINER_CONSTRAINT = "container_constraint"
    PRIMING_TRIM_UNSUPPORTED = "priming_trim_unsupported"
    AV_DURATION_MISMATCH = "av_duration_mismatch"
    INTERNAL_ERROR = "internal_error"


class Severity(str, Enum):
    ERROR = "error"       # 必须转码，禁止直拼
    WARNING = "warning"   # 计划可生成但需注意（如音频尾部填充）


class Finding(BaseModel):
    """单条判定依据：哪条流、哪个样本、为什么。"""

    code: FailureCode
    severity: Severity
    stream: Optional[MediaType] = None
    clip_index: Optional[int] = None
    sample_index: Optional[int] = None
    message: str
    evidence: dict = Field(default_factory=dict)


class PlannedSample(BaseModel):
    """输出计划中的单个样本映射（逐样本可检验）。"""

    clip_index: int
    stream: MediaType
    src_index: int                 # 源流包序号
    src_dts: int                   # 源 DTS ticks（源流时基）
    src_pts: int
    out_dts: int                   # 输出 DTS ticks（输出容器时基）
    out_pts: int
    duration: int                  # 输出时基 ticks
    keyframe: bool
    role: Literal["content", "preroll_reference", "silence_pad", "drop_encoder_delay"] = "content"
    # 音频：该包对输出贡献的 PCM 样本（扣除 encoder delay / 追加 padding 后）
    pcm_samples_contribution: Optional[int] = None
    note: str = ""


class SegmentPlan(BaseModel):
    """单个片段在拼接输出中的重定位结果。"""

    clip_index: int
    source: str
    stream: MediaType
    output_start_dts: int
    output_start_pts: int
    preroll_count: int = Field(0, description="为参考而保留的剪切点前样本数（头/尾参考）")
    content_count: int
    pad_count: int = Field(0, description="音频尾部静音填充包数")
    first_content_src_index: Optional[int] = None
    # 该段实际保留的全部源包序号（含预滚/尾部参考），供逐样本核对引用闭包
    retained_src_indices: list[int] = Field(default_factory=list)
    samples: list[PlannedSample]


class ConcatPlan(BaseModel):
    """完整拼接计划。feasible=False 时 mode=transcode 且 segments 为空。"""

    job_id: str
    feasible: bool
    mode: Literal["concat_copy", "transcode_required"]
    output_container: str
    output_time_base: tuple[int, int]
    clips: list[str]
    findings: list[Finding] = Field(default_factory=list)
    segments: list[SegmentPlan] = Field(default_factory=list)
    output_duration_sec: float = 0.0
    # 供校验的汇总不变量
    invariants: dict = Field(default_factory=dict)

    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == Severity.ERROR]
