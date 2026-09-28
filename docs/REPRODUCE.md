# 复现文档

## 1. 环境

- Linux x86_64，Python 3.12.3；ffmpeg/ffprobe 存在但本项目不依赖它们
  （输入为本地合成描述符）。
- 所有依赖安装在项目内 `.venv`；完整版本锁定见 `requirements.txt`。

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

直接依赖（`requirements.in`）：fastapi 0.141.1、numpy 2.5.3、uvicorn 0.54.0、
httpx 0.28.1、pytest 9.1.1（pydantic 2.13.5 随 fastapi 安装）。

## 2. 重新生成夹具（可选，夹具已提交）

```bash
.venv/bin/python fixtures/generate_fixtures.py
```

夹具均为手工可算的确定性样本表：视频 1/90000 时基、3000 tick/帧（恰 30 fps）、
2 层 B 帧重排（源 DTS 从 −6000 起）；音频 1/48000、1024 样本/帧的 AAC-LC。

| 夹具 | 用途 |
|---|---|
| `seg_ok_a.json` / `seg_ok_b.json` | 兼容直拼；B 音频短 640 样本，触发 1 帧尾部填充；各 1024 延迟 |
| `seg_midgop.json` | 非关键帧裁剪（裁剪窗口 2/15s–4/15s 落在 GOP 中部） |
| `seg_open_gop.json` | 开放 GOP：K6 为非 IDR 恢复点，B7/B8 引用恢复点之前的 P3 |
| `seg_audio_delay.json` | 2048 样本（两帧）编码器延迟 |
| `seg_tb_mismatch.json` | 视频时基 1/25 → TIMEBASE_MISMATCH |
| `seg_codec_mismatch.json` | hevc vs h264 → CODEC_MISMATCH |
| `seg_dangling_ref.json` | `depends_on` 指向不存在的索引 99 → MISSING_REFERENCE |

## 3. 运行测试

```bash
.venv/bin/python -m pytest
# 结果（已保存 docs/results/pytest-output.txt）：53 passed
```

测试组织：

- `test_timebase.py` — 手算有理数换算（如 3000@1/90000 = 1600@1/48000），
  不可整除换算必须报 INPUT_ERROR；
- `test_compat.py` — 时基/编码不一致返回精确类别且 `requirement=transcode`；
- `test_gop.py` — 中部裁剪闭包 `{0,1,4,5,6,7,8}`、开放 GOP 闭包 `{0,1,4,7,8,9}`
  且索引 1 作为预滚保留、悬空引用报 MISSING_REFERENCE；
- `test_audio.py` — priming 集合、填充帧数与合成 DTS/PTS（如 16 帧音频对
  16000 tick 视频 → 恰好 1 帧填充，pad dts=16384/pts=15360）；
- `test_planner.py` — 端到端逐样本断言完整 DTS/PTS 序列（20 个视频样本
  out_dts = 0..57000，out_pts 全序列硬编码）、裁剪预滚、开放 GOP、两帧延迟、
  转码决策、参考缺失；
- `test_validate.py` — 对计划做 8 种定向破坏，断言各自的失败类别；
- `test_api.py` — 真实 HTTP：正常完成、转码决策、失败作业 422/404/409、
  进度步骤、复验。

所有期望值为测试内手工硬编码，不调用被测核心生成参考答案。

## 4. 启动服务并调用

```bash
scripts/run_server.sh                 # APP_PORT 可改端口，默认 8000
```

健康与版本：

```bash
curl -s http://127.0.0.1:8000/health
curl -s http://127.0.0.1:8000/version
```

提交正常作业（直拼 + 非关键帧裁剪）：

```bash
curl -s -X POST http://127.0.0.1:8000/jobs -H 'content-type: application/json' -d '{
  "segments": [
    {"path": "seg_ok_a.json"},
    {"path": "seg_ok_b.json"}
  ]
}'
```

异常作业（开放 GOP 裁剪 / 时基不兼容 / 悬空参考）：

```bash
curl -s -X POST http://127.0.0.1:8000/jobs -H 'content-type: application/json' -d '{
  "segments": [{"path": "seg_open_gop.json", "trim_in": {"num": 1, "den": 5}}]
}'
curl -s -X POST http://127.0.0.1:8000/jobs -H 'content-type: application/json' -d '{
  "segments": [{"path": "seg_ok_a.json"}, {"path": "seg_tb_mismatch.json"}]
}'
curl -s -X POST http://127.0.0.1:8000/jobs -H 'content-type: application/json' -d '{
  "segments": [{"path": "seg_dangling_ref.json"}]
}'
```

随后取逐样本计划、进度与复验（`$JOB_ID` 为上一步返回值）：

```bash
curl -s http://127.0.0.1:8000/jobs/$JOB_ID/plan
curl -s http://127.0.0.1:8000/jobs/$JOB_ID/events
curl -s -X POST http://127.0.0.1:8000/jobs/$JOB_ID/validate
```

端到端批量示例（8 个正常/异常案例，结果落 `var/demo/`）：

```bash
APP_BASE=http://127.0.0.1:8000 .venv/bin/python scripts/demo_client.py
```

## 5. 已保存的可复核结果（run 26d4dc382af0）

`docs/results/` 中保存了一次真实服务运行：

- `pytest-output.txt` — 53 个测试逐条 PASSED 的完整输出；
- `run-26d4dc382af0.log` — JSON 行日志（run_id/job_id/sha256/步骤/版本）；
- `demo-summary.json` — 8 个案例的 HTTP 状态、作业状态、decision、错误类别；
- `example-direct-concat.json` — 正常直拼作业的完整逐样本计划与复验 `ok:true`；
- `example-missing-reference.json` — 悬空参考作业：`status=failed`、
  `error_category=MISSING_REFERENCE`（未伪装成功）；
- `server-startup.txt` — uvicorn 启动输出。

8 个案例实测结果：

| 案例 | HTTP | 作业状态 | decision | 错误类别 |
|---|---|---|---|---|
| 01 直拼 | 201 | completed | direct_concat | — |
| 02 GOP 中部裁剪 | 201 | completed | direct_concat | — |
| 03 开放 GOP 裁剪 | 201 | completed | direct_concat | — |
| 04 音频 2048 延迟 | 201 | completed | direct_concat | — |
| 05 时基不兼容 | 201 | completed | transcode_required | TIMEBASE_MISMATCH |
| 06 编码不兼容 | 201 | completed | transcode_required | CODEC_MISMATCH/PARAM_MISMATCH |
| 07 悬空参考 | 201 | **failed** | failed | MISSING_REFERENCE |
| 08 文件缺失 | 422 | — | — | INPUT_ERROR |

## 6. 受约束容器规则（mp4-constrained/v1，校验器逐条实现）

R1 输出 DTS 非负；R2 DTS 严格单调；R3 视频 PTS≥DTS；R4 音频负 PTS 不得早于
edit-list priming 下界；R5 保留样本的全部 `depends_on` 必须在计划中；R6 呈现
样本（含音频填充）连续覆盖裁剪窗口；R7 padding 仅允许作为音频片段尾部连续段；
R8 每片段视频解码序列首样本必须是关键帧。
