# 离线 PCM 双阈值静音切段后端

离线接收 PCM / WAV 音频流，基于**双阈值滞回（hysteresis）状态机**做静音检测，
输出**保留的原始样本区间** `[start, end)`（以混合单声道后的帧序号计，即样本序号）。

- 进入静音 / 退出静音使用**不同阈值**与各自的**最小持续时间**
- 判定状态**跨输入块延续**，输出与切块方式无关（切块不变性）
- 短噪声不割裂长静音；邻近区间合并规则明确
- 输出区间保证不重叠、不越界、样本守恒
- FastAPI + NumPy + SQLite；作业状态、事件与原始块持久化，支持失败重放与独立复核
- 仅依赖本地合成夹具，无需任何外部账号或业务数据

## 1. 目录结构

```
app/
  errors.py     # 错误分类（输入/状态冲突/资源耗尽/计算失败）与统一错误信封
  timing.py     # 时间<->样本换算
  kernel.py     # 时间与信号内核：双阈值滞回状态机（流式 RLE 实现）
  media.py      # 媒体解析：WAV / 原始 PCM -> float64 单声道 [-1,1]
  schemas.py    # 配置 / 请求 / 响应契约（pydantic）
  store.py      # SQLite 作业状态、事件、原始块
  service.py    # 业务编排：解析 -> 内核 -> 持久化 -> 区间输出
  web.py        # FastAPI 路由、错误处理器、run_id 中间件
  config.py     # 环境变量配置
scripts/
  make_fixtures.py  # 生成本地合成 WAV 夹具
tests/
  reference_segmenter.py      # 独立参考实现（逐样本循环，刻意不使用被测内核）
  test_kernel_hand_verified.py# 手工核验区间：阈值脉冲/跨块长静音/全静音/末尾未完成段
  test_kernel_invariants.py   # 切块不变性、样本守恒、合并规则、流式锁定
  test_reference_crosscheck.py# 随机信号：被测内核 vs 独立参考实现
  test_media.py               # 媒体解析契约与各类畸形输入
  test_store.py               # 作业状态机与资源耗尽
  test_api.py                 # HTTP 端到端、错误码分类、重放事件
  test_verify.py              # 复核接口
```

## 2. 算法契约（务必先读）

设样本振幅绝对值 `level = |x|`，两个阈值满足
`0 <= enter_threshold < exit_threshold <= 1`（线性幅度；dB 换算 `amp = 10**(db/20)`）。

状态机在样本序列上产生交替的运行段（run）：

| 当前状态 | 维持条件 | 翻转条件 |
|---|---|---|
| `S`（静音侧） | `level < exit_threshold`（含中间带 `[enter, exit)`） | `level >= exit_threshold` |
| `H`（语音侧） | `level >= enter_threshold`（含中间带） | `level < enter_threshold` |

中间带样本**保持原状态**，构成滞回，避免阈值附近抖动反复翻转。

运行段二级规则（标签与相邻段无关，独立判定后合并同标签邻段）：

- `H` 段长度 `>= min_speech` 才判为语音；否则为**短噪声**，标签改判静音
  —— 两侧长静音不会被一个短脉冲割裂。
- `S` 段长度 `>= min_silence` 才确认静音并产生切分；否则短静缝标签改判语音，
  两侧语音连为一段。但一个语音区域必须**至少包含一个 raw=H 段**：纯 S 段被
  改判只是"桥接"，全程从未越过 `exit_threshold` 的流（即使总长短于
  `min_silence`）产出为空，不会凭空保留。
- 边界策略 `edge_keep=true`（默认）：与流首尾相接的 `H` 段即使不足
  `min_speech` 也保留（流外没有后续静音来证明它是噪声）；`edge_keep=false`
  时按严格规则丢弃。末尾未完成段的行为由此显式定义，两种行为都有测试断言。

区间生成：

1. 合并连续同标签段，得到语音区域 `[a, b)`。
2. 加前后保留量（按样本计）：`[a - pad_before, b + pad_after)`。
3. 相邻区间满足 `next.start <= prev.end + merge_gap` 则合并
   （`merge_gap` 默认 0，即只在接触/重叠时合并；也可显式配置）。
4. clamp 到 `[0, total_samples)`。

流式锁定规则（保证切块不变性）：当前为正在增长的 `S` 段时，只有已观测到的
静音长度 `G >= max(min_silence, pad_before + pad_after + merge_gap + 1)`，
其左侧区间才会被提交——此时该静音既已确认，padding 也不可能再与未来语音相连。
未锁定的区间在 `GET` 中标记为未提交；`finalize` 后输出与一次性处理完全相同。

## 3. 环境与复现

依赖版本（Python 3.12）：

```
fastapi==0.115.6
uvicorn[standard]==0.34.0
numpy==2.2.1
pydantic==2.13.5
httpx==0.28.1
pytest==8.3.4
```

（httpx 仅测试期 TestClient 经由 starlette 需要；运行时 Web 栈为
fastapi/uvicorn/pydantic/numpy，存储为标准库 sqlite3。）

从干净目录：

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 生成合成夹具（写入 data/fixtures/）
.venv/bin/python scripts/make_fixtures.py

# 全部测试
.venv/bin/pytest -q

# 启动服务（默认 127.0.0.1:8000，SQLite 在 ./data/jobs.db）
.venv/bin/uvicorn app.web:app --host 127.0.0.1 --port 8000
```

配置（环境变量，均有默认值）：

| 变量 | 默认 | 含义 |
|---|---|---|
| `SEG_DATA_DIR` | `./data` | SQLite 与夹具目录 |
| `SEG_MAX_SAMPLES_PER_JOB` | `8000000` | 单作业最大样本（帧数），超出返回 413 |
| `SEG_MAX_CHUNK_BYTES` | `8388608` | 单请求体最大字节数，超出返回 413 |
| `SEG_MAX_JOBS` | `200` | 最多作业数，超出返回 413 |
| `SEG_EVENT_RING` | `2000` | 单作业保留事件数（环形截断旧事件） |

## 4. API 与请求样例

每个响应/日志都带 `run_id`（请求级唯一编号），错误响应结构：

```json
{"error": {"code": "INPUT_INVALID", "message": "...", "run_id": "...", "details": {...}}}
```

错误分类：`JOB_NOT_FOUND`(404)、`INPUT_INVALID`(422)、`STATE_CONFLICT`(409)、
`RESOURCE_EXHAUSTED`(413)、`COMPUTATION_FAILED`(500)。

### 4.1 原始 PCM 流式作业（跨块）

```bash
J=$(curl -s -X POST http://127.0.0.1:8000/jobs \
  -H 'Content-Type: application/json' \
  -d '{
    "config": {
      "enter_threshold": 0.03, "exit_threshold": 0.08,
      "min_silence_ms": 100, "min_speech_ms": 50,
      "pad_before_ms": 10, "pad_after_ms": 20,
      "merge_gap_ms": 0, "edge_keep": true
    },
    "media": {"container": "pcm", "sample_format": "s16",
              "sample_rate": 16000, "channels": 1}
  }' | .venv/bin/python -c 'import sys,json;print(json.load(sys.stdin)["job_id"])')

# 任意切块上传；?finalize=true 可在最后一块直接收尾
curl -s -X POST "http://127.0.0.1:8000/jobs/$J/chunks" \
  -H 'Content-Type: application/octet-stream' --data-binary @part1.pcm
curl -s -X POST "http://127.0.0.1:8000/jobs/$J/finalize"

curl -s http://127.0.0.1:8000/jobs/$J            # 状态 + 区间
curl -s http://127.0.0.1:8000/jobs/$J/events     # run_id/中间状态/判定理由
curl -s -X POST http://127.0.0.1:8000/jobs/$J/verify  # 独立复核
```

支持的 `sample_format`：`s16` / `s24` / `s32`（小端有符号整型）、`f32`（小端
float32）；多声道按各声道幅度均值混合。块长必须帧对齐，尾部不足一帧返回 422
并给出 `bytes_consumed` / `remainder`。

### 4.2 WAV 一次性作业

WAV（PCM 16/24/32-bit 整型或 32-bit float，1–8 声道）在单个请求中上传并自动收尾：

```bash
curl -s -X POST "http://127.0.0.1:8000/jobs" \
  -H 'Content-Type: application/json' \
  -d '{"config": {"enter_threshold": 0.03, "exit_threshold": 0.08},
       "media": {"container": "wav"}}'
# -> job_id，再 POST /jobs/{id}/chunks 上传完整 WAV；或直接：
curl -s -X POST \
  "http://127.0.0.1:8000/segment?enter_threshold=0.03&exit_threshold=0.08&min_silence_ms=100" \
  -H 'Content-Type: application/octet-stream' \
  --data-binary data/fixtures/near_threshold_pulses.wav
```

### 4.3 区间响应

```json
{
  "job_id": "...", "status": "finalized",
  "sample_rate": 1000, "total_samples": 1200, "duration_ms": 1200.0,
  "intervals": [
    {"start": 0, "end": 220, "start_ms": 0.0, "end_ms": 220.0}
  ],
  "stats": {"num_intervals": 1, "kept_samples": 220, "dropped_samples": 980}
}
```

## 5. 失败重放

- 每个请求分配 `run_id`，出现在响应信封、结构化日志、`events` 表中。
- `GET /jobs/{id}/events` 保留：请求（方法/路径/run_id）、每个 run 的
  `start/end/length/标签/判定理由`、区间锁定与收尾事件、错误事件。
- 原始块按 `chunk_seq` 持久化，`POST /jobs/{id}/verify` 重放全部样本：
  用**独立参考实现**（逐样本循环，非内核 RLE）核对语义结果，再用多种切块
  重新跑内核核对切块不变性，并校验不越界/不重叠/样本守恒。
- 进程重启后：已 finalize 作业从 SQLite 完整恢复；open 作业的已锁定区间从
  `committed_json` 恢复，续写新块时服务端会先从持久化块重放重建状态机，
  区间与不重启完全一致（见 `tests/test_verify.py` 的
  `test_open_job_survives_restart_and_continues_chunks`）。

## 6. 验收结果（干净目录实测记录）

环境：Python 3.12.3 / Linux，依赖按上面版本锁定安装。

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python scripts/make_fixtures.py
.venv/bin/pytest -q
# => 322 passed, 1 warning in 7.92s   （warning 为 starlette 内部 anyio 别名弃用提示）
```

线上冒烟（另开终端起 uvicorn 后）：

```bash
.venv/bin/python scripts/smoke.py
# finalize: finalized intervals= [[0, 220], [590, 820]]
# stats: kept_samples=450 dropped_samples=750
# verify: chunking_invariance / integrity / sample_conservation /
#         reference_semantics 全部 True
# 对已收尾作业再写块 -> 409 STATE_CONFLICT
```

四类夹具经 `POST /segment` 实测区间：

| 夹具 | 样本数 | 输出区间 |
|---|---|---|
| near_threshold_pulses | 1200 | `[0,220) [490,600) [890,1020)`（中间带脉冲不翻转、短噪声被吞并） |
| cross_chunk_silence | 1200 | `[0,220) [590,820)` |
| all_silence | 500 | `[]`（kept=0, dropped=500） |
| tail_unfinished | 750 | `[390,750)`（尾部语音保留并 clamp） |
