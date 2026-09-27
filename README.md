# PCM 积分响度与响度范围（LRA）后端 — EBU R128

一个从零实现的多模块 Python 后端，对 **48 kHz PCM** 计算符合标准的**积分（门控）响度**
与**响度范围（Loudness Range, LRA）**，并提供 FastAPI 的一次性 / 分块（流式）接口。
所有验证数据均为本地确定性合成夹具；参考答案来自**与被测内核相互独立**的两个实现
（本地 `ffmpeg ebur128` C 实现、第三方 `pyloudnorm`），不使用生产账号或真实素材。

- 标准：**ITU-R BS.1770-4** + **EBU R 128 v4** + **EBU Tech 3342 (2016)**
- 技术栈：Python 3.12 · FastAPI · NumPy · SciPy（仅用 `lfilter` 原语）· SQLite · pytest
- 内核版本：`1.0.0`（响应、日志、`/health` 均可见）

---

## 1. 明确限定的方法（处理顺序）

每个常量都在 `app/r128_constants.py` 中带条款来源，内核里没有"魔法数字"。

```
解码后的 PCM (float64, 按任意大小分块到达)
 │
 ├─ ① K 加权（逐声道，IIR 状态跨分块保持）
 │     Stage 1: 高架"预滤波" (high-shelf, 奈奎斯特 +4 dB, f0≈1682 Hz)
 │     Stage 2: RLB 二阶高通 (Butterworth 形态, fc≈38 Hz)
 │     顺序固定：先 shelf，后 RLB
 │
 ├─ ② 切分为 100 ms 帧（4800 样本）；不足 100 ms 的尾部样本缓存到下一块，
 │     结束时仍不足者剔除并在 warnings / trailing_samples_dropped 中报告
 │
 ├─ ③ 声道权重（作用在均方能量上）：
 │     L/R/C = 1.0，环绕 Ls/Rs/Lb/Rb = 1.41（≈+1.5 dB），
 │     DualMono = 2.0（≈+3.01 dB），LFE = 0（不计入响度和）
 │
 ├─ ④ 积分门控块：400 ms，75% 重叠（每 100 ms 一个块）
 ├─ ⑤ LRA 块：3.0 s 窗，按 **1.0 s 步进**记录（重叠 2/3，
 │     满足 EBU Tech 3342 "重叠不超过 2/3"；与 ffmpeg/libebur128 一致）
 │
 ├─ ⑥ 积分响度两级门控（严格按顺序）：
 │     绝对门  Γa = −70 LUFS
 │     相对门  Γr = −0.691 + 10·log10(Σ Gc·mean(z_c,[高于绝对门的块])) − 10 LU
 │     最终保留"严格高于两者"的块；I = −0.691 + 10·log10(加权平均能量)
 │
 └─ ⑦ LRA（Tech 3342）：
       3 s 块先过 −70 LUFS 绝对门；
       相对门 =（通过绝对门的 3 s 块能量均值）− 20 LU；
       0.1 LU 等宽直方图（箱 i 代表值 −69.95+0.1i LUFS）；
       秩 p_lo=⌊(N−1)·0.10+0.5⌋、p_hi=⌊(N−1)·0.95+0.5⌋，箱内不插值；
       LRA = 第 95 百分位箱响度 − 第 10 百分位箱响度（LU）
```

**这不是 "RMS + 偏移" 冒充门控响度。** 有专门的反证测试：同 RMS 的 40 Hz 与
1 kHz 正弦，经 K 加权 / RLB 后响度相差 > 6 LU（`test_kweighting_makes_equal_rms_differ_in_loudness`）。

### 静音与不足窗口：专门状态，绝不伪造数字

ffmpeg 在静音时会打印 `I=-70.0`、阈值 `0.0`——那是**哨兵值而非测量结果**。本后端区分：

| 状态 | 触发条件 |
|---|---|
| `SILENCE` | 全部样本为数字零（无完整块，或有块但无一过门） |
| `NOT_COMPUTED` | 有完整 400 ms 块，但**全部低于 −70 LUFS**；或 LRA 无块过相对门（信号真实但过轻） |
| `INSUFFICIENT_BLOCKS` | 没有完整 400 ms 块（时长 <400 ms）；或不足 3 s 无法形成 LRA 块 |
| `OK` | 门控后产生数值 |

积分响度与 LRA 各有独立子状态：例如 1 s 信号积分有效但 LRA 为 `INSUFFICIENT_BLOCKS`。

### 真峰值（True Peak）：明确声明**不支持**

内核没有过采样级，因此 `true_peak = "NOT_MEASURED"`，并在每条结果的 `uncertainties` 中
重复声明。**本系统不会把采样峰值称为真峰值，也不在任何地方声称支持 TP。**

---

## 2. 模块组织

| 文件 | 职责 |
|---|---|
| `app/media.py` | 媒体解析：手工 RIFF/WAVE 解析（PCM/IEEE float/EXTENSIBLE、16/24/32-bit、f32/f64）、交错 PCM 解码、残余字节 |
| `app/filters.py` | 时间/信号内核：流式 K 加权（两级、跨块 `zi/zf` 状态） |
| `app/loudness.py` | 滑窗、帧能量、声道权重、400 ms/3 s 块、两级门控、LRA 直方图、状态机 |
| `app/jobstore.py` | 作业状态：SQLite 持久化（元数据/结果/错误类别/计时），音频字节不落盘 |
| `app/jobs.py` | 流式作业编排：分块 PCM、帧对齐、meter 生命周期 |
| `app/main.py` | 验证接口：FastAPI 路由、统一信封、错误分类、请求关联 |
| `app/r128_constants.py` | 全部标准常量与状态 |
| `app/config.py` / `errors.py` / `schemas.py` / `serialization.py` / `logging_setup.py` | 配置、错误类别、请求模型、结果序列化、结构化日志 |
| `tests/` | 独立测试与合成夹具（`fixtures.py`）、参考实现（`reference.py`） |
| `scripts/compare_reference.py` | 与 ffmpeg / pyloudnorm 的独立对比，输出门控前后块数与参数 |

---

## 3. 本地启动

需要本机有 Python 3.12 与（用于参考校验的）`ffmpeg`。应用运行本身只依赖 venv。

```bash
# 1) 创建虚拟环境并安装锁定依赖
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
# （已附 requirements.lock，可用 .venv/bin/pip freeze 核对）

# 2) 启动（默认 http://127.0.0.1:8000，SQLite 在 ./r128_jobs.db）
scripts/run_local.sh
#   可选环境变量：R128_DB_PATH、R128_MAX_JOB_BYTES

# 3) 示例请求（健康检查 / WAV / 分块流 / 静音）
examples/example_requests.sh
# 端口被占用时：BASE=http://127.0.0.1:8123 examples/example_requests.sh
```

## 4. HTTP 接口

所有响应携带 `request_id`（可用请求头 `X-Request-ID` 指定，否则自动生成并在
响应头回显）、`kernel_version`、`specification`、`processed_at`（处理位置，形如
`pid=1234@r128-local`）。失败返回稳定的 `error.code`（见下表）。

| 方法/路径 | 说明 |
|---|---|
| `GET /health` | 版本、标准标识、DB 路径、处理位置 |
| `POST /analyze/wav` | 上传完整 WAV（octet-stream），一次性测量 |
| `POST /jobs` | 创建分块作业；body `{"channels":2,"sample_format":"s16","roles":[...]}`，roles 可省略 |
| `POST /jobs/{id}/chunks` | 追加原始交错 PCM 字节（任意长度，可跨样本/跨帧） |
| `POST /jobs/{id}/finalize` | 结束并返回完整结果 |
| `GET /jobs/{id}` | 取持久化结果 / 错误 / 计时 |
| `GET /jobs` | 最近作业（可观测） |

原始 PCM `sample_format`：`s16` / `s24` / `s32` / `f32` / `f64`（小端）。
声道：`1`（单声道，权重 1.0）、`2`（L R）、`6`（5.1，ITU/SMPTE 顺序 L R C LFE Ls Rs）；
显式 `roles` 可覆盖（如 `DualMono`、`Ls/Rs/Lb/Rb`、`LFE`）。

错误类别（`error.code`，测试逐一断言）：`UNSUPPORTED_FORMAT`(415)、
`UNSUPPORTED_SAMPLE_RATE`(422)、`UNSUPPORTED_LAYOUT`(422)、`INVALID_MEDIA`(400)、
`INVALID_LAYOUT`(400)、`JOB_ERROR`(409，未知/已关闭作业、超限、重启后状态丢失)。

### 结果字段（节选）

```jsonc
{
  "status": "OK",
  "integrated_loudness_lufs": -22.995,
  "true_peak": "NOT_MEASURED",
  "integrated_gating": {
    "blocks_total": 47, "blocks_above_absolute_gate": 47,
    "blocks_above_relative_gate": 47,
    "absolute_gate_lufs": -70.0, "relative_gate_lufs": -32.99,
    "ungated_mean_lufs": -22.99, "absolute_gated_mean_lufs": -22.99 },
  "loudness_range": {
    "status": "OK", "lra_lu": 18.0,
    "blocks_total": 30, "blocks_above_absolute_gate": 30,
    "blocks_above_relative_gate": 30, "relative_gate_lufs": -42.4,
    "percentile_10_lufs": -34.7, "percentile_95_lufs": -16.7,
    "gated_histogram_LUFS_rep_to_count": {"-34.6": 3, "...": "…"} },
  "signal": {"duration_seconds": 5.0, "sample_peak_linear": 0.1001,
             "trailing_samples_dropped": 0, "sample_rate_hz": 48000},
  "channel_layout": ["L"], "channel_weights_energy": [1.0],
  "warnings": [], "uncertainties": ["true-peak ... NOT_MEASURED"],
  "method": { "lra_overlap": "2/3 ...", "relative_gate_lra_lu": -20.0, "...": "…" }
}
```

日志为每行一条 JSON（stdout），带 `request_id` / `job_id`；失败记 `request_failed`，
部分结果记 `partial_result`，关键步骤记 `wav_parsed` / `job_created` / `job_finalized`。

---

## 5. 如何验证

```bash
# 单元 + 接口测试（63 项，含与 ffmpeg/pyloudnorm 的具体数值断言）
.venv/bin/python -m pytest -q

# 独立参考对比（打印门控前后块数、各门限、百分位；写 reports/reference_comparison.json）
.venv/bin/python scripts/compare_reference.py
```

夹具：数字静音、校准恒定正弦（单/立体声）、200/250 ms 短突发、<400 ms / 1 s /
3 s 边界、低于 −70 的非静音噪声、左右声道切换、5.1 环绕 + 强 LFE、四级电平多段节目。

测试断言**具体数值与具体失败类别**，不是"接口能调通"：

- 校准音：单声道 −23.0 / 立体声 −20.0 LUFS，与 ffmpeg 差 ≤0.05 LU；相对门 −33/−30；
- 250 ms 突发：`blocks 17 → abs 6 → rel 6`，I=−26.8 LUFS（滑窗跨越，ffmpeg 一致）；
- 时长 0.30/0.39 s → 0 个完整块 → `INSUFFICIENT_BLOCKS`；0.40/0.50 s → 1/2 块；
- 多段节目：I=−20.4、门 −31.6、块 `317 → 252 → 186`；LRA=18.0 LU、p10=−34.7/p95=−16.7、
  LRA 门 −42.4，与 ffmpeg 全部吻合；
- 5.1：权重 `[1,1,1,0,1.41,1.41]`，强 LFE 不改变结果（差 <1e-9）；DualMono 比单声道高 3.01 dB；
- **分块一致性**：同一段信号按多种帧边界、以及按 1499 字节（跨 4 字节立体声帧）的原始
  字节流喂入，积分/LRA/块数/直方图与整段一次喂入逐位一致（容差 1e-12）。

**参考答案不来自被测内核自身**：`ffmpeg`（独立 C 实现，权威门控/LRA 依据）+
`pyloudnorm`（独立第三方积分响度代码路径）。滤波器常量还在 `test_filters.py` 中与
pyloudnorm 的规范级 "DeMan" 模拟原型设计相互核对（≤2e-7），并核对 DC=0 dB、奈奎斯特 +4 dB。

---

## 6. 支持范围与关键取舍（务必阅读）

**支持**
- 仅 **48 kHz**（规范唯一名义采样率）。非 48 kHz 返回 `UNSUPPORTED_SAMPLE_RATE`
  并提示先重采样（如 `ffmpeg -ar 48000`）；**系统不做重采样**，避免重采样误差污染计量。
- WAV：PCM (0x0001)、IEEE float (0x0003)、EXTENSIBLE (0xFFFE 的 PCM/float 子类型)；
  16/24/32-bit int、32/64-bit float；1/2/6 声道（显式 roles 时声道数更宽松）。
- 流式原始 PCM：上述采样格式、小端、交错、任意分块大小。
- 积分门控响度、LRA、门控前后块数与各门限、声道权重、采样峰值（非真峰值）。

**不支持 / 显式不做**
- **真峰值（BS.1770 过采样 TP）：不测量、不声称。**
- 压缩/非 PCM 容器（MP3/AAC/FLAC/Opus 等）：`UNSUPPORTED_FORMAT`。
- 非 48 kHz、非本地素材、网络拉流、自动重采样。
- RF64/BWF 超大 RIFF、非标准 chunk 顺序的罕见变体（标准 RIFF/WAVE 正常解析）。

**实现取舍**
- 环绕权重采用 **1.41**（能量域，`10log10(1.41)=1.492 dB`）而非解析值 1.41254：
  这是 ffmpeg/libebur128 与 pyloudnorm 共用的两位小数因子，保证跨工具一致。
- LRA 采用 **3 s 窗 / 1 s 步进（2/3 重叠）**与 ffmpeg 一致（Tech 3342 的最大允许重叠）。
  注意：pyloudnorm 0.2.0 自带的 LRA 用 97% 重叠 + 线性插值，是其自身对 Tech 3342 的
  不同诠释，与本实现及 ffmpeg 有系统差异；因此**只把 pyloudnorm 用作积分响度参考，
  不用作 LRA 参考**。
- IIR 数值原语使用 `scipy.signal.lfilter(zi/zf)`（TDF-II、跨块传状态）；滤波器系数、
  分块、滑窗、加权、门控、LRA 直方图全部为本项目自有实现，并用"流式 vs 整段"一致性
  测试严格约束。为最小依赖没有引入 C 扩展，超长信号的 Python 分帧为 O(N)。
- SQLite 存元数据/结果，**音频字节不落盘**；流式 meter 状态在进程内存，进程重启后
  未完成作业返回明确 `JOB_ERROR`（而非给出半截数字）。

## 7. 依赖锁定

- `requirements.txt`：运行依赖（fastapi、uvicorn、numpy、scipy、pydantic）
- `requirements-dev.txt`：测试/参考依赖（pytest、httpx、pyloudnorm==0.2.0）
- `requirements.lock`：`pip freeze` 全量精确版本
