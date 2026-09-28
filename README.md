# 媒体拼接样本边界规划（Media Concat Sample-Boundary Planner）

本地音视频片段**无重编码拼接（stream-copy concat）可行性分析**后端。
输入片段的真实容器/编码元数据，输出受限容器（MP4 / MPEG-TS）下**逐样本**
的拼接计划；不可直拼时给出**具体失败类别并要求转码**，绝不伪装直拼、
绝不把异常/未知状态统一返回成功。

- 技术栈：Python 3.12 · FastAPI · NumPy（整数有理时间换算）· SQLite（作业状态）
- 输入：本地 sidecar 合成夹具（默认，零外部依赖）+ 可选 ffprobe 真实媒体适配器
- 输出：`ConcatPlan`（可行性、模式、逐样本 DTS/PTS 重定位、预滚、priming、
  尾部填充、判定依据 findings、校验不变量）

## 目录结构

```
mediaconcat/
  models.py          # 核心数据模型（与 ffprobe 无关的最小公共子集）
  kernel.py          # 时间与信号内核：有理整数时基换算、DTS/PTS 重定位、
                     # priming 扣减、尾部填充、解码参考闭包（NumPy）
  media.py           # 媒体解析：sidecar 夹具 + ffprobe 适配器（同一模型）
  planner.py         # 规划器：兼容性判定、裁剪解析、预滚/重排/填充、不变量校验
  jobstore.py        # SQLite 作业状态机 queued→planning→succeeded|failed
  service.py         # 服务编排（未知异常显式 failed，不吞错）
  api.py / api_models.py / config.py / logging_setup.py / main.py
scripts/
  build_fixtures.py     # 独立夹具构建器（不 import 被测核心）
  make_real_fixtures.sh # 可选：ffmpeg 生成真实 MP4
fixtures/               # 最小合成 sidecar（*.media.json）
tests/                  # 独立测试，断言具体结果与失败类别
examples/client_demo.py # 服务调用示例
logs/                   # JSONL 结构化运行日志（按 run_id/job_id 关联）
requirements.txt        # 依赖锁定（含传递依赖）
```

## 快速复现

```bash
# 1) 环境（已锁定版本）
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 2) 生成最小数据夹具（独立构建器）
.venv/bin/python scripts/build_fixtures.py

# 3) 运行测试（独立测试：手工期望，非被测核心自证）
.venv/bin/python -m pytest -q

# 4) 启动服务
.venv/bin/python -m mediaconcat.main            # http://127.0.0.1:8000

# 5) 调用示例（另开终端）
.venv/bin/python examples/client_demo.py
```

### 真实媒体端到端（可选，需要本机 ffmpeg/ffprobe）

```bash
bash scripts/make_real_fixtures.sh
MEDIACONCAT_ALLOW_FFPROBE=1 MEDIACONCAT_RUN_REAL=1 \
  .venv/bin/python -m pytest tests/test_real_media.py -q
```

## API

| 方法 & 路径 | 说明 |
| --- | --- |
| `GET /health` | 版本与 run_id |
| `POST /jobs` | 提交规划作业 `{sources, output_container, cuts?}`（同步返回 job_id） |
| `GET /jobs` | 作业列表 |
| `GET /jobs/{id}` | 作业详情 + 完整计划（成功时）或 error_code/error（失败时） |
| `GET /jobs/{id}/verify` | 逐样本校验视图（非负 DTS、严格递增、各角色计数、错误/警告分类） |

curl 示例：

```bash
curl -s -X POST localhost:8000/jobs -H 'content-type: application/json' \
  -d '{"sources":["tb_mixed_a","tb_mixed_b"],"output_container":"mp4"}'
curl -s localhost:8000/jobs/<job_id>/verify | python3 -m json.tool
```

## 规划语义（可测试行为）

1. **编码/参数/时基/关键帧兼容（保守：无法确认一致即拒绝直拼）**
   - 编码、分辨率、像素格式、profile/level、SAR、SPS/PPS（`extradata_id`）
     不一致 → `codec_mismatch` / `video_param_mismatch`；音频采样率、声道、
     AAC object type、AudioSpecificConfig 不一致 → `audio_param_mismatch`。
   - 输出时基为所有源时基的**精确 LCM**；换算出现余数，或音频 PCM 帧长在输出
     时基下非整数 ticks（如 AAC@44100→1/90000）→ `timebase_incompatible`。
     时间戳换算超 int64 → `timebase_conversion_overflow`；LCM 超 uint32 →
     `timebase_incompatible`。MPEG-TS 时钟固定 1/90000，不被源分母整除 →
     `container_constraint`（如 1/12800）。
   - **每个拼接边界**（无论是否显式 `cut_in`）的首内容帧都必须是**干净 IDR**
     （关键帧且不引用任何帧）。自引用/引用更早（开放 GOP、CRA）/引用更晚帧的
     关键帧都不是合法边界 → `non_keyframe_cut`；参考闭包跨出片段或指向上一片段
     → `open_gop_reference_lost`。
2. **解码/呈现时间重定位 + 必要预滚**
   - 所有输出时间戳用整数有理换算（`NumPy`），逐样本保留
     `out_pts - out_dts == scale(src_pts - src_dts)`（B 帧重排不丢失）。
   - 裁剪按**呈现序（PTS）**定界再映射回解码序（正确处理 B 帧）；非帧/PCM
     边界（精确有理数判定，不使用浮点容差）→ `invalid_range`。
   - 首片段允许从非关键帧起切：参考闭包沿 P 链**完整回溯到最近的干净 IDR**
     （开放 GOP 会跨 GOP 拉入所需帧），这些帧标 `preroll_reference`，bitstream
     从输出 DTS 0 开始（无负 DTS），呈现由 edit-list 隐藏；MPEG-TS 不支持负
     PTS，此类预滚在 TS 下 → `container_constraint`。拼接边界则一律拒绝。
   - ffprobe 适配器不暴露逐帧参考列表（`reference_info_complete=false`），
     无法确认边界为闭合 GOP；因此**真实媒体多片段拼接**在边界保守判
     `gop_structure_unknown` 要求转码（单片段 / 显式 sidecar 不受影响）。
3. **音频 priming、尾部填充与多轨 A/V 对齐**
   - AAC priming（如 2112 = 2×1024 + 64）逐包扣减，标记
     `drop_encoder_delay` 与逐包可听贡献（0/0/960/1024…）。MP4 edit-list
     可表达首段 priming；MPEG-TS/MKV（本模型不承载 CodecDelay）无法表达 →
     `container_constraint`；**后续片段**带 priming 时单一 edit-list 无法逐段
     平移 → `priming_trim_unsupported`。
   - 多片段两遍装配：先确定视频拼接边界，音频再用静音帧对齐到同一边界
     （`silence_pad`，末帧可为部分贡献帧精确收边、绝不越过边界），保证跨片段
     A/V 同步，给 `av_duration_mismatch` 警告；可听音频**长于**视频（需删音频包
     才能对齐）时直拼不可行 → `av_duration_mismatch` 错误。
4. **仅生成计划也逐样本可检验，且校验独立**
   - 每个输出样本都有源 index、源/输出 DTS、PTS、duration、关键帧标志与角色。
   - `verify` 独立重算：非负/严格单调（含**重复 DTS** 拒绝）、段间不重叠
     （按 `dts+duration` 比较）、重排偏移保持、无损换算余数、以及用**独立朴素
     DFS** 重算参考闭包（不复用规划路径）核对每个保留帧（含 preroll）的引用。

## 失败类别（Finding.code）

`codec_mismatch` · `timebase_incompatible` · `timebase_conversion_overflow` ·
`video_param_mismatch` ·
`audio_param_mismatch` · `non_keyframe_cut` · `open_gop_reference_lost` ·
`negative_dts` · `container_constraint` · `invalid_range` · `empty_input` ·
`input_not_found` · `gop_structure_unknown` · `av_duration_mismatch(可短填充时为警告/音频过长时为错误)` ·
`priming_trim_unsupported` · `internal_error`

severity 为 `error` 时计划降级为 `mode=transcode_required, feasible=false`；
`warning` 不阻断直拼但会明确列出条件。

## 日志可关联性与可复核性

- 每次运行有固定 `run_id`（可用 `MEDIACONCAT_RUN_ID` 注入），日志落
  `logs/<run_id>.jsonl`，每行含 `ts/level/event/run_id/job_id/source/version`
  以及 `progress/step/decision/basis`（失败判定同样记录 decision 与错误码）。
- 作业表持久化输入快照、状态、plan_json 或 error_code+traceback。
- 已保存一次真实 HTTP 层运行的正常与异常结果：
  ```bash
  .venv/bin/python scripts/verify_run.py     # 重新生成 logs/verified_run/
  ```
  其中 `summary.txt` 汇总 9 个作业（4 个可行直拼、4 个必须转码、1 个输入失败），
  `*.verify.json` 为逐样本校验视图，`run.jsonl` 为可按 job_id 关联的结构化日志，
  `pytest.txt` 为完整测试输出（77 passed，含真实媒体用例）。

## 配置（环境变量）

`MEDIACONCAT_HOST/PORT/DB/FIXTURES/LOG_DIR/LOG_LEVEL`、
`MEDIACONCAT_ALLOW_FFPROBE=1`（开启真实媒体解析）、
`MEDIACONCAT_MPEGTS_CLOCK`（默认 90000）、`MEDIACONCAT_MAX_TIMESCALE`
（默认 2³²−1）。

## 设计边界（诚实声明）

- 规划器只做“能否无重编码拼接”的样本边界分析，不实际调用 ffmpeg 合成文件。
- ffprobe 单包字段不暴露开放 GOP 的跨帧引用关系，真实媒体路径保守按闭合
  GOP 处理；开放 GOP、AAC priming 的**精确行为以合成夹具为权威**并由其测试。
- “仅生成计划”在 MPEG-TS 下若无法表达 priming/时基，会明确要求转码。
