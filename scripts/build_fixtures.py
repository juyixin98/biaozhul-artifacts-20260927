#!/usr/bin/env python3
"""合成夹具构建器（独立于被测核心：不 import mediaconcat）。

直接构造 ffprobe 风格的流/包元数据 sidecar JSON。所有时间戳均为
手工设计的整数序列，便于测试中写入独立的期望常量。
覆盖：混合时基、开放 GOP、AAC 编码器延迟、非关键帧裁剪、A/V 尾部填充、
MPEG-TS 时钟不兼容。
"""
from __future__ import annotations

import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
FIX = ROOT / "fixtures"


def frame(i, dts, pts, dur, key=False, refs=None, pcm=None):
    f = {"index": i, "dts": dts, "pts": pts, "duration": dur,
         "keyframe": key, "references": refs or []}
    if pcm is not None:
        f["pcm_samples"] = pcm
    return f


def video_stream(codec, tb, frames, w=320, h=240, container="mp4"):
    return {
        "codec": codec, "codec_type": "video", "time_base": list(tb),
        "start_time_ticks": 0, "width": w, "height": h,
        # 合成夹具统一编码参数（S2 兼容性检查的实质输入）
        "pixel_format": "yuv420p", "profile": "high", "level": 31,
        "sample_aspect_ratio": "1/1", "extradata_id": "synth-sps-avc1-320x240",
        "container": container, "frames": frames,
    }


def audio_stream(tb, packets, rate=48000, channels=2, delay=0, codec="aac",
                 container="mp4"):
    return {
        "codec": codec, "codec_type": "audio", "time_base": list(tb),
        "start_time_ticks": 0, "sample_rate": rate, "channels": channels,
        "audio_object_type": "AAC-LC", "profile": "LC",
        "extradata_id": f"synth-asc-{rate}-{channels}",
        "encoder_delay_samples": delay, "container": container, "frames": packets,
    }


def write(name, doc):
    FIX.mkdir(exist_ok=True)
    p = FIX / f"{name}.media.json"
    p.write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print("wrote", p.relative_to(ROOT))


def bb_pattern_25():
    """25fps, 时基 1/25，7 帧 I B B P B B P（含重排，闭合 GOP）。"""
    # 解码序: I0 P3 B1 B2 P6 B4 B5
    order = [(0, 0, True, []), (3, 1, False, [0]), (1, 2, False, [0, 3]),
             (2, 3, False, [0, 3]), (6, 4, False, [3]), (4, 5, False, [3, 6]),
             (5, 6, False, [3, 6])]
    return [frame(i, dts=i, pts=pts, dur=1, key=key, refs=refs)
            for i, (pts, dts, key, refs) in enumerate(order)]


def bb_pattern_30():
    """30fps, 时基 1/30，同形状。"""
    return bb_pattern_25()  # ticks 相同、时基不同


def linear_p(n, tb_den, key_at=0, open_after=None, fps_num=1):
    """线性 P 链（无 B 帧），dts=pts=i*step。"""
    frames = []
    for i in range(n):
        refs = []
        key = i == key_at or (key_at and False)
        if i == 0:
            key = True
        if open_after is not None and i == open_after + 1:
            refs = [open_after, open_after - 1]
        elif i > 0:
            refs = [i - 1]
        frames.append(frame(i, dts=i, pts=i, dur=1, key=key, refs=refs))
    return frames


def aac_packets(n, tb_den=48000, pcm=1024):
    return [frame(i, dts=i * pcm, pts=i * pcm, dur=pcm, key=True, pcm=pcm)
            for i in range(n)]


def main():
    # 1) 混合时基（25 vs 30 fps），闭合 GOP + B 帧重排
    write("tb_mixed_a", {"source": "fixtures/tb_mixed_a.media.json",
                         "video": video_stream("h264", (1, 25), bb_pattern_25())})
    write("tb_mixed_b", {"source": "fixtures/tb_mixed_b.media.json",
                         "video": video_stream("h264", (1, 30), bb_pattern_30())})

    # 2) 开放 GOP：0..4 闭合链；5 为开放 GOP IDR；6 同时引用 5 与上一 GOP 的 4。
    #    从帧6（非关键帧，0.24s）起切：既要求非关键帧预滚，又必须保留跨 GOP 参考帧4。
    gop = linear_p(10, 25)
    gop[5] = frame(5, 5, 5, 1, key=True, refs=[])
    gop[6] = frame(6, 6, 6, 1, key=False, refs=[5, 4])
    gop[7] = frame(7, 7, 7, 1, refs=[6])
    gop[8] = frame(8, 8, 8, 1, refs=[7])
    gop[9] = frame(9, 9, 9, 1, refs=[8])
    write("open_gop", {"source": "fixtures/open_gop.media.json",
                       "cut_in_sec": 0.24,
                       "video": video_stream("h264", (1, 25), gop)})

    # 3) 跨片段开放 GOP 引用丢失：b 的帧1引用 -1（上一片段最后一帧）
    a = linear_p(5, 25)
    b = linear_p(5, 25)
    b[0] = frame(0, 0, 0, 1, key=True, refs=[])
    b[1] = frame(1, 1, 1, 1, refs=[0, -1])
    write("open_gop_lost_a", {"source": "fixtures/open_gop_lost_a.media.json",
                              "video": video_stream("h264", (1, 25), a)})
    write("open_gop_lost_b", {"source": "fixtures/open_gop_lost_b.media.json",
                              "video": video_stream("h264", (1, 25), b)})

    # 4) AAC 编码器延迟 2112（=2*1024+64），8 个 1024 样本包
    write("aac_delay", {"source": "fixtures/aac_delay.media.json",
                        "audio": audio_stream((1, 48000), aac_packets(8), delay=2112)})
    # MPEG-TS 变体：音频时基 1/90000，包长换算为 1920 ticks（1024 PCM）
    ts_packets = [frame(i, dts=i * 1920, pts=i * 1920, dur=1920, key=True, pcm=1024)
                  for i in range(8)]
    write("aac_delay_ts", {"source": "fixtures/aac_delay_ts.media.json",
                           "audio": audio_stream((1, 90000), ts_packets,
                                                 rate=48000, delay=2112,
                                                 container="mpegts")})

    # 5) 非关键帧裁剪：b 从帧2（非关键帧）起切，位于拼接边界
    write("nonkey_a", {"source": "fixtures/nonkey_a.media.json",
                       "video": video_stream("h264", (1, 25), linear_p(5, 25))})
    write("nonkey_b", {"source": "fixtures/nonkey_b.media.json",
                       "cut_in_sec": 0.08,
                       "video": video_stream("h264", (1, 25), linear_p(5, 25))})

    # 6) A/V 尾部填充：视频 8 帧=0.32s，音频 12 包=0.256s
    write("av_tailpad", {
        "source": "fixtures/av_tailpad.media.json",
        "video": video_stream("h264", (1, 25), linear_p(8, 25)),
        "audio": audio_stream((1, 48000), aac_packets(12), delay=0),
    })

    # 7) 1/12800 时基：mp4 可行，mpegts 因 90000 不被整除而拒绝直拼
    frames12800 = [frame(i, i * 512, i * 512, 512, key=(i == 0),
                         refs=[] if i == 0 else [i - 1]) for i in range(3)]
    write("tb12800", {"source": "fixtures/tb12800.media.json",
                      "video": video_stream("h264", (1, 12800), frames12800)})


if __name__ == "__main__":
    main()
