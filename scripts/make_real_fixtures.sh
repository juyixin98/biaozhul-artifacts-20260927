#!/usr/bin/env bash
# 用真实 ffmpeg 生成两个不同帧率/时基的本地 MP4（合成信号，无业务数据）。
# 产出供 tests/test_real_media.py 的可选适配器测试使用。
set -euo pipefail
DIR="$(dirname "$0")/../fixtures/real"
mkdir -p "$DIR"

# testsrc 25fps，2s；强制 closed-GOP（每帧无 B 帧，-g 25）
ffmpeg -hide_banner -loglevel error -y \
  -f lavfi -i "testsrc=size=320x240:rate=25:duration=2" \
  -c:v libx264 -pix_fmt yuv420p -g 25 -bf 0 \
  -video_track_timescale 25 -an "$DIR/real_a_25fps.mp4"

# testsrc 30fps，2s
ffmpeg -hide_banner -loglevel error -y \
  -f lavfi -i "testsrc=size=320x240:rate=30:duration=2" \
  -c:v libx264 -pix_fmt yuv420p -g 30 -bf 0 \
  -video_track_timescale 30 -an "$DIR/real_b_30fps.mp4"

echo "generated:"
ls -la "$DIR"
