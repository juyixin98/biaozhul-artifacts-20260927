# EBU R128 积分响度与响度范围（LRA）测量后端

本地、离线的合成 PCM 响度分析服务。对 **PCM 数据**按明确限定的 **EBU R128 /
ITU-R BS.1770-4 / EBU Tech 3341 / EBU Tech 3342** 方法计算：

- **积分响度（Integrated Loudness）**，单位 LUFS（带绝对门控 + 相对门控）；
- **响度范围（Loudness Range, LRA）**，单位 LU（P95 − P10）；
- 附带瞬态（0.4 s）与短期（3 s）分块响度、门控前后块数、各门限等中间统计。

> 不做真峰值（true peak）测量，因此**任何结果中都不声称 true peak**：
> `true_peak_tpbs` 恒为 `null`，`true_peak_supported=false`，
> 并在 `uncertainties` 中列出 `TRUE_PEAK_NOT_MEASURED_NOT_CLAIMED`。

技术栈：Python 3.12、FastAPI、NumPy（测量内核仅依赖 NumPy；K 加权滤波用 SciPy
`lfilter` 携带 `zi/zf` 状态实现流式）、SQLite。无外部账号、无生产数据、无网络
依赖；所有音频夹具均为本地合成。

---

## 1. 测量方法（顺序固定、可审计）

处理管线严格按以下顺序，任何参数都可在响应的 `parameters` / `gate_stats` 中核对：

1. **媒体解析**（`app/media.py`）：无压缩 WAV（u8/s16/s24/s32 PCM、f32 IEEE
   float）或带头描述符的裸 PCM → float64，约 ±1.0。
2. **K 加权滤波**（`app/kernel/filter.py`，Tech 3341 第 1 阶段），逐声道、零初始
   状态，顺序为：
   1. RBJ 高频搁架双二阶：增益 **+4.0 dB**，Q = 1/√2，f_c = **1500 Hz**；
   2. RBJ 高通双二阶：Q = 0.5，f_c = **38 Hz**。
3. **声道权重**（BS.1770-4）：`[L, R, C, Ls, Rs]` 的增益 G 为
   `[1.0, 1.0, 1.0, 1.41, 1.41]`（环绕声道 ≈ +3.01 dB）。
   **LFE 被剔除，不参与测量**。支持 mono / stereo / 5.0 / 5.1。
4. **分块均方能量**（重叠滑动窗，非整窗丢弃，不用 RMS 冒充门控响度）：
   - 瞬态块（用于积分响度）：T_g = **0.400 s**，跳幅 **0.100 s**；
   - 短期块（用于 LRA）：T_g = **3.000 s**，跳幅 **0.100 s**；
   - 块能量 z_c = 窗内每声道平方均值；块响度 L_j = **−0.691 + 10·log10(Σ_c G_c·z_c)**。
5. **积分响度门控**（Tech 3341，严格先绝对后相对）：
   1. **绝对门限 Γ_a = −70 LUFS**，保留 L_j ≥ −70 的块；
   2. 由绝对门控后块的**能量均值**（不是 dB 均值）计算未门控响度；
   3. **相对门限 Γ_r = 该响度 − 10 LU**；
   4. 取同时满足 L_j > Γ_r **且** L_j > Γ_a 的块，再做能量均值 → 积分 LUFS。
6. **LRA 门控**（Tech 3342，在短期块上独立进行）：
   1. 绝对门限 −70 LUFS；
   2. 相对门限 = 绝对门控短期块积分响度 **−20 LU**；
   3. 对门控后块响度取 **P95 − P10**（NumPy 默认线性插值分位数）。

### 静音 / 不足窗口的专门状态

| 场景 | 顶层 `status` | 子状态 |
|---|---|---|
| 不足一个 0.4 s 瞬态块 | `INSUFFICIENT_BLOCKS` | integrated=`INSUFFICIENT_BLOCKS`，lra 同 |
| 有块但全部 < −70 LUFS | `SILENCE` | integrated/lra=`SILENCE`，数值为 `null` |
| 积分可算、不足 3 s 短期块 | `INTEGRATED_OK_LRA_NOT_APPLICABLE` | lra=`INSUFFICIENT_BLOCKS` |
| 短期块 < 30 个（< ~6 s 有效素材） | `OK_WITH_WARNINGS` | `LRA_LOW_CONFIDENCE_FEW_SHORTTERM_BLOCKS` |
| 正常 | `OK` | — |

### 分块重放一致性

`StreamingMeter.push()` 可接受任意大小的数据块；滤波延迟线随块传递。**同一 PCM
无论整段喂入还是任意切块（包括 1 个样本、与窗/跳幅不对齐的尺寸），逐块响度、
门控计数与最终 LUFS/LRA 完全一致**——由 `tests/test_chunking.py` 逐块断言。

---

## 2. 目录结构

```
app/
  config.py       环境变量配置、R128 常量、算法版本号
  media.py        媒体解析：WAV/裸 PCM → float64；声道布局映射；LFE 剔除
  kernel/
    filter.py     K 加权（搁架+高通）流式双二阶
    meter.py      分块调度、声道权重、绝对/相对门控、LRA、状态分类
  validation.py   入参类型化校验（稳定失败码）
  jobs.py         SQLite 作业状态机（PENDING/PROCESSING/SUCCEEDED/FAILED）
  service.py      测量编排（解析层与内核的唯一执行路径）+ 结果信封
  log.py          结构化 JSON 日志（关联 request_id/job_id/步骤/版本/worker）
  api.py / main.py FastAPI 接口与 ASGI 入口
tests/            独立测试（93 个），含独立参考实现与外部对拍
scripts/          夹具生成、跨实现对拍脚本
examples/         curl 示例与合成 WAV 夹具（生成产物）
```

内核没有硬编码演示值：所有数字来自信号计算；`config.py` 中的常量即标准参数。

---

## 3. 本地启动

需要 Python 3.12（开发环境版本）。

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt          # 运行时
# 或完全复现已验证环境：
.venv/bin/pip install -r requirements-dev.lock     # 含测试/对拍依赖

# 生成合成夹具
.venv/bin/python scripts/generate_fixtures.py

# 启动（默认 127.0.0.1:8000）
.venv/bin/python -m app.main
# 或： .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
```

环境变量（均有本地默认值）：`R128_DB_PATH`（默认 `./r128_jobs.db`）、
`R128_WORKER_ID`（处理位置标识，默认 `local-worker`）、
`R128_MAX_PAYLOAD_BYTES`（默认 64 MiB）。

---

## 4. 接口

| 方法/路径 | 说明 |
|---|---|
| `GET /health` | 存活、`algorithm_id`、`worker_id` |
| `GET /version` | 标准引用与能力矩阵（明确 true peak / 压缩格式不支持） |
| `POST /measurements/wav` | 上传无压缩 WAV，同步返回完整结果 |
| `POST /measurements/pcm` | 裸小端 PCM（表单：`sample_rate, channels, sample_format`） |
| `POST /jobs` | 上传 WAV 创建异步作业（本地同步执行，状态落 SQLite） |
| `GET /jobs/{id}` | 作业状态 + 结果或失败类别 |
| `GET /jobs?limit=` | 最近作业 |

所有端点支持 `X-Request-Id` 请求头做请求关联；未提供时生成 UUID，响应体与日志
都带同一标识。失败统一为 `{"request_id","error":{"code","message","step"}}`，
失败码例如：`WAV_NOT_RIFF`、`WAV_COMPRESSED_UNSUPPORTED`、
`WAV_UNSUPPORTED_BIT_DEPTH`、`UNSUPPORTED_CHANNEL_LAYOUT`、`LAYOUT_CHANNEL_MISMATCH`、
`UNSUPPORTED_SAMPLE_RATE`、`UNSUPPORTED_PCM_FORMAT`、`PCM_TRUNCATED`、
`WAV_TRUNCATED_DATA`、`PAYLOAD_TOO_LARGE`、`JOB_NOT_FOUND`。

### 示例请求

```bash
# 恒音：积分 ≈ -9.07 LUFS，LRA = 0
curl -s -X POST http://127.0.0.1:8000/measurements/wav \
  -F "payload=@examples/fixtures/constant_tone.wav;type=audio/wav" \
  -F "include_blocks=true" -H 'X-Request-Id: demo-1'

# 静音：status=SILENCE，LUFS/LRA 均为 null
curl -s -X POST http://127.0.0.1:8000/measurements/wav \
  -F "payload=@examples/fixtures/silence.wav;type=audio/wav"

# 裸 s16le PCM
curl -s -X POST http://127.0.0.1:8000/measurements/pcm \
  -F "payload=@/tmp/tone.s16;type=application/octet-stream" \
  -F "sample_rate=48000" -F "channels=1" -F "sample_format=s16"
```

更多见 `examples/requests.sh`。结果字段含义：

- `signal`：采样率、布局、源声道数、分析声道数、声道权重、时长；
- `parameters`：块长/跳幅、各门限、丢弃的尾部样本数；
- `integrated_loudness.gate_stats`：`total_blocks / above_absolute_gate /
  above_both_gates / relative_gate_lufs / selected_mean_power`（门控前后块数）；
- `loudness_range`：`lra_lu`、P10/P95、短期块门控统计、`block_loudness_lufs`
  （`include_blocks=true` 时）；
- `provenance`：`algorithm_id`、标准引用、`worker_id`、处理耗时、是否分块。

日志为单行 JSON（stderr），字段含 `ts/level/service/worker_id/algorithm_id/
step/request_id/job_id/msg/context`；失败为 warning 行并单列 `code`，不确定结论
在响应 `uncertainties` 中单独列出。

---

## 5. 验证方式与已验证结果

### 测试

```bash
.venv/bin/pip install -r requirements-dev.lock
.venv/bin/python -m pytest tests/ -q
```

**93 passed**（实测，约 110 s）。测试不是“接口能调通”式检查，而是断言具体结果与
失败类别，例如：

- 静音 → `SILENCE` 且数值为 `null`；200 ms → `INSUFFICIENT_BLOCKS` 且尾部样本数正确；
- 恒音积分 = −9.0656 LUFS（0.5 FS、1 kHz），LRA = 0；
- 0.5 s 短突发：57 个瞬态块 → 绝对门控后 6 个 → 相对门控后 5 个，
  Γ_r ≈ −21.41 LUFS，门控积分 ≈ −10.61 LUFS，并断言与全段 RMS 代理相差 > 5 LU；
- 20 s 两级信号 LRA ≈ 20 LU；环绕声道比前声道高 10·log10(1.41) dB；LFE 被忽略；
- 多种切块尺寸（1/7/100/4800/4096/47999/200000 样本）逐块等于整段；
- 失败码：压缩/非 WAV、位深、声道数、布局不一致、采样率、负载截断/超限。

### 参考答案不是被测内核自产

三个相互独立的参照：

1. **`tests/independent_reference.py`**：测试侧从零重写的 EBU 实现——独立的 RBJ
   系数推导、对完整信号一次性滤波、逐窗直接求能量（不走生产代码）；
2. **pyloudnorm 0.2.0**（第三方包）；
3. **ffmpeg `ebur128`** CLI（外部二进制，存在时自动对拍，缺失则 `skip`）。

对拍脚本输出门控前后块数、门限和四方数值：

```bash
.venv/bin/python scripts/cross_reference_check.py
```

实测（节选）：

| 夹具 | 本实现 I | 独立参考 | pyloudnorm | ffmpeg | 本实现 LRA | 独立参考 | ffmpeg |
|---|---|---|---|---|---|---|---|
| 静音 6 s | null(SILENCE) | null | -inf | ≤ −70（钳到门限） | null | null | 0.0 |
| 恒音 8 s | −9.07 | −9.07 | −9.07 | −9.0 | 0.0 | 0.0 | 0.0 |
| 短突发 | −10.61 | −10.61 | −10.61 | −10.6 | 5.59 | 5.59 | 7.0* |
| 声道变化 5.1 | −8.13 | −8.13 | −8.13 | −8.1 | 0.0 | 0.0 | 0.0 |

积分响度与 pyloudnorm/独立参考在 1e-9 LU 内一致；恒音/动态信号 LRA 与 ffmpeg 在
0.1–0.5 LU 内一致。

### 已知参考差异（显式记录，不掩盖）

- **pyloudnorm 0.2.0 的 LRA**：其 `loudness_range` 在滤波前给信号**追加 1.5 s
  静音**且采用约 90 ms 跳幅，末尾滤波器振铃块进入分布，使恒音/短素材 LRA 系统性
  偏高约 **1.41 LU**（动态长素材无影响）。ffmpeg 与本实现按 Tech 3342 使用
  100 ms 跳幅、不追加静音。测试中该偏差被显式锁定
  （`test_pyloudnorm_known_lra_discrepancy_is_documented`），pyloudnorm 仅作为
  **积分响度**的精确参考与 LRA 的有界 sanity check。
- **短突发 LRA vs ffmpeg（5.59 vs 7.0 LU）**：来自末端窗口约定（流式最后一帧）与
  ffmpeg 将响度量化进 0.1 LU 直方图的实现差异；Tech 3342 允许的容差为 1 LU 量级，
  稳态素材无差异。对拍脚本对该夹具使用更宽容差并标注。
- ffmpeg 对纯静音的摘要输出钳为 `I: -70.0 LUFS / LRA: 0.0`（而非 -inf）。

---

## 6. 支持范围与关键取舍

**支持**：mono / stereo / 5.0 / 5.1（LFE 剔除）；采样率 8/12/16/22.05/24/32/
44.1/48/88.2/96/192 kHz；WAV 的 u8/s16/s24/s32 PCM 与 f32；裸 s16/s24/s32/f32；
积分响度、LRA、瞬态/短期块响度与完整门控统计。

**不支持（明确拒绝，不静默猜测）**：

- **真峰值 BS.1770-4 true peak**（无 4× 过采样表计）——结果恒为 `null`；
- 压缩媒体（MP3/AAC/Opus/…）、大端 RIFX、7.1/其他声道布局；
- 裸 8 位 PCM（WAV 容器内支持 u8）。

**取舍**：

- 分位数用 NumPy 线性插值，与 EBU 参考直方图插值在稀疏分布上有微小差异；
- 短于一个窗的尾部样本丢弃（标准做法），并在参数中报告丢弃样本数；
- 作业在本地进程内同步执行（SQLite 状态机真实持久化），没有外部消息代理；
- 滤波器零初始状态（假设信号起于静止），首块含起振瞬态，属标准行为。

---

## 7. 依赖锁定

- `requirements.txt`：运行时直接依赖及版本区间；
- `requirements.lock`：运行时精确冻结版本；
- `requirements-dev.lock`：含 pytest、pyloudnorm、httpx、SciPy 测试用途的全量冻结。
