"""媒体解析层。

两条输入路径产出同一个 :class:`ClipProbe` 模型：

1. **sidecar 合成夹具**（默认、无外部二进制依赖）：与片段同名的
   ``.media.json``，字段即流/包元数据，用于确定性测试。
2. **ffprobe 适配器**（需显式开启 ``MEDIACONCAT_ALLOW_FFPROBE=1``）：
   对真实本地媒体运行 ffprobe，提取同样的最小子集。

裁剪请求（cut_in/cut_out 秒）在 sidecar 顶层或 API 请求中给出。
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from .config import Settings, settings as default_settings
from .logging_setup import LogBinding, bind
from .models import ClipProbe, FrameRecord, StreamProbe


class ProbeError(RuntimeError):
    pass


def parse_sidecar(path: str | Path) -> ClipProbe:
    p = Path(path)
    if not p.exists():
        raise ProbeError(f"sidecar 夹具不存在: {p}")
    data = json.loads(p.read_text(encoding="utf-8"))
    return ClipProbe.model_validate(data)


def _ffprobe_json(path: Path) -> dict:
    binary = shutil.which("ffprobe")
    if binary is None:
        raise ProbeError("ffprobe 不可用，且输入不是 sidecar 夹具")
    cmd = [
        binary,
        "-v",
        "error",
        "-show_streams",
        "-show_packets",
        "-of",
        "json",
        str(path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False)
    if proc.returncode != 0:
        raise ProbeError(f"ffprobe 失败 ({proc.returncode}): {proc.stderr.strip()}")
    return json.loads(proc.stdout or "{}")


def _parse_time_base(raw: Optional[str]) -> tuple[int, int]:
    if not raw or raw == "0/0":
        return (1, 1)
    num, den = raw.split("/")
    return (int(num), int(den))


def parse_real_media(path: str | Path) -> ClipProbe:
    """ffprobe 适配器：真实容器 → ClipProbe。

    提取可得的编码参数（像素格式/profile/level/SAR、extradata 哈希、音频
    profile/object type），供规划器做保守一致性判定。

    局限：ffprobe 单包字段不直接给出逐帧参考列表，开放 GOP 的跨帧引用无法
    据此恢复（包 ``references`` 为空）。因此**真实媒体多片段拼接**在无法确认
    引用闭合时，由规划器以“首帧必须是干净 IDR”保守把关；开放 GOP 的精确
    行为仍以合成夹具为权威。
    """
    import hashlib

    p = Path(path)
    doc = _ffprobe_json(p)
    streams_out: dict[str, StreamProbe] = {}
    packets_by_stream: dict[int, list[FrameRecord]] = {}

    for st in doc.get("streams", []):
        codec_type = st.get("codec_type")
        if codec_type not in ("video", "audio"):
            continue
        index = int(st["index"])
        tb = _parse_time_base(st.get("time_base"))
        kwargs = dict(
            codec=st.get("codec_name", "unknown"),
            codec_type=codec_type,  # type: ignore[arg-type]
            time_base=tb,
            frames=[],
            container=p.suffix.lstrip(".").lower() or "mp4",
        )
        # SPS/PPS、AudioSpecificConfig 等 codec extradata 的一致性标识
        extradata = st.get("extradata")
        if extradata:
            kwargs["extradata_id"] = hashlib.sha1(
                bytes.fromhex(extradata)).hexdigest()[:16]
        if codec_type == "video":
            kwargs.update(
                width=int(st.get("width", 0)),
                height=int(st.get("height", 0)),
                pixel_format=st.get("pix_fmt"),
                profile=st.get("profile"),
                level=int(st["level"]) if st.get("level") not in (None, -1, "-1") else None,
                sample_aspect_ratio=st.get("sample_aspect_ratio"),
            )
        else:
            kwargs.update(
                sample_rate=int(st.get("sample_rate", 0)),
                channels=int(st.get("channels", 0)),
                profile=st.get("profile"),
                audio_object_type=st.get("profile"),
            )
        stream = StreamProbe(**kwargs)
        # ffprobe 不提供逐帧参考列表：引用结构不完整，多片段拼接需保守处理
        stream.reference_info_complete = False
        streams_out[codec_type] = stream
        packets_by_stream[index] = []

    type_by_index = {
        int(st["index"]): st.get("codec_type")
        for st in doc.get("streams", [])
        if st.get("codec_type") in ("video", "audio")
    }
    counters: dict[int, int] = {}
    for pkt in doc.get("packets", []):
        sidx = int(pkt["stream_index"])
        if sidx not in type_by_index:
            continue
        dts = int(pkt.get("dts", pkt.get("pts", 0)))
        pts = int(pkt.get("pts", dts))
        dur = int(pkt.get("duration", 0)) or 0
        if dur <= 0:
            # 跳过无 duration 的包时不得预占 index，避免与列表位置错位（L5）
            continue
        i = counters.get(sidx, 0)
        counters[sidx] = i + 1
        is_key = "K" in (pkt.get("flags") or "")
        ctype = type_by_index[sidx]
        rec = FrameRecord(
            index=i,
            dts=dts,
            pts=pts,
            duration=dur,
            # 音频包都是独立可解码的“关键帧”；视频严格按 ffprobe 的 K 标志
            keyframe=True if ctype == "audio" else is_key,
        )
        packets_by_stream.setdefault(sidx, []).append(rec)

    for sidx, packets in packets_by_stream.items():
        ctype = type_by_index[sidx]
        stream = streams_out[ctype]
        stream.frames = packets
        if packets:
            stream.start_time_ticks = packets[0].dts

    return ClipProbe(
        source=str(p),
        video=streams_out.get("video"),
        audio=streams_out.get("audio"),
    )


def resolve_clip(
    source: str,
    settings: Settings = default_settings,
    log: Optional[LogBinding] = None,
) -> ClipProbe:
    """解析输入标识：先按 sidecar 夹具解析，失败且允许时回退 ffprobe。"""
    log = log or bind()
    candidates = [Path(source)]
    if not source.endswith(".json"):
        candidates.append(Path(settings.fixtures_dir) / f"{source}.media.json")
        candidates.append(Path(settings.fixtures_dir) / source)

    for cand in candidates:
        if cand.suffix == ".json" and cand.exists():
            log.step("probe", "parse_sidecar", source=source, resolved=str(cand))
            return parse_sidecar(cand)

    if settings.allow_ffprobe and Path(source).exists():
        log.step("probe", "ffprobe", source=source)
        return parse_real_media(source)

    raise ProbeError(
        f"无法解析输入 {source!r}：在 {settings.fixtures_dir} 下未找到 sidecar 夹具，"
        "且未开启真实媒体解析（MEDIACONCAT_ALLOW_FFPROBE=1）"
    )
