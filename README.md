# 离线 PCM 双阈值静音切段后端

对离线单声道 PCM/WAV 音频做**双阈值迟滞**静音检测，输出需要保留的**原始样本区间**
（半开 `[start, end)`，单位为样本）。全部本地运行，合成夹具，无外部账号或业务数据。

- 技术栈：Python 3.12 · FastAPI · NumPy · SQLite（标准库 `sqlite3`）· Pytest/HTTPX
- 进入/退出静音使用**不同阈值**与**不同最小持续时间**，前后保留量按样本计
- 判定状态**跨输入块延续**，输出与切块大小无关（切块不变性）
- 长静音中的短噪声不会割裂静音；邻近区间按明确规则合并
- 输出区间保证不重叠、不越界；样本计数守恒

---

## 1. 判定规则（信号内核）

逐样本按幅度 `|x|` 分三带（`enter <= exit`）：

| 带 | 条件 | 含义 |
|---|---|---|
| LOW | `|x| < enter_threshold` | 足够安静，累计“进入静音” |
| LOUD | `|x| >= exit_threshold` | 足够响，累计“退出静音” |
| MID | 其余 | 迟滞带，不推动任何一方，并冲掉双方计数 |

- **进入静音**：ACTIVE 状态下连续 LOW 达到 `min_silence` 个样本才切换；区间尾点
  锚定到这段 LOW 的起点（判定用最小持续时间，切点不被推迟到确认时刻）。
- **退出静音**：SILENT 状态下连续 LOUD 达到 `min_activity` 个样本才切换；新区间
  起点回溯到这段 LOUD 的起点。
- **短噪声不割裂长静音**：SILENT 中不足 `min_activity` 的 LOUD 被随后的 LOW/MID
  冲掉，不产生区间、不改变静音。
- **末尾未完成段**：结束时停在 ACTIVE，则把未确认成静音的尾部保留到音频末尾；
  停在 SILENT 不产生尾段。
- **后处理**：对原始区间两端各保留 `pad` 个样本 → 裁到 `[0, total)` →
  当相邻区间间隔 `<= merge_gap` 个样本时合并（负值即重叠也合并）→ 去除空区间。

所有时间量对外用毫秒、对内由时间内核确定性换算成整数样本（向下取整）。

---

## 2. 工程结构（模块边界与数据/错误契约）

```
app/
  errors.py        错误契约：8 个错误码 -> 4 个可区分类别 + HTTP 状态
  config.py        扁平 YAML 配置 + 同名大写环境变量覆盖
  timing.py        时间内核：毫秒<->样本，确定性换算
  media.py         媒体解析：WAV(s16/f32, 单声道) / 裸 s16le、f32le -> float64
  segmentation.py  信号内核：流式双阈值状态机 + 保留/合并/裁剪 + 不变量校验
  store.py         作业状态：SQLite（CAS 状态转换）+ 原始字节落盘
  logging_utils.py 运行日志：JSONL，run_id + 关键中间状态 + 判断理由
  service.py       编排：解析 -> 换算 -> 流式判定 -> 落库/日志；/verify 核验
  schemas.py       Pydantic 请求模型
  api.py           FastAPI 路由与统一错误信封
  main.py          装配入口
scripts/           合成夹具 + 真实 HTTP 演示
tests/             89 个测试，含独立标量 oracle（不引用被测内核）
```

错误类别互不相交，测试逐一断言：

| code | category | HTTP | 触发举例 |
|---|---|---|---|
| `INVALID_ARGUMENT` | input | 400 | 阈值次序错、时长为负、裸流缺 sample_rate、配置非 JSON |
| `MEDIA_PARSE_ERROR` | input | 400 | 非 RIFF/WAVE、立体声、不支持的位深、奇数长度裸 s16 |
| `EMPTY_INPUT` | input | 400 | 0 字节 / 解析出 0 样本 |
| `RESOURCE_EXHAUSTED` | resource | 413 | 超字节预算或样本预算 |
| `STATE_CONFLICT` | state | 409 | 同幂等键不同载荷；对非 PENDING 作业重复执行 |
| `JOB_NOT_FOUND` | state | 404 | 作业/运行不存在 |
| `COMPUTATION_FAILED` | computation | 422 | NaN/Inf 样本、区间不变量被破坏 |
| `INTERNAL` | internal | 500 | 未归类异常 |

错误响应统一为 `{"error": {"code", "category", "message", "details"}}`。

---

## 3. 从干净目录复现

### 3.1 安装（固定版本见 requirements.txt）

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

实测版本：numpy 2.5.3 / fastapi 0.141.1 / uvicorn 0.54.0 / python-multipart 0.0.32 /
pytest 9.1.1 / httpx 0.28.1（Python 3.12.3）。

### 3.2 配置

默认值见 `config.yaml`；任一项可用同名大写环境变量覆盖，例如：

```bash
export MAX_SAMPLES_PER_JOB=200000 PORT=8077
```

关键项：`min_silence_ms=300`、`min_activity_ms=100`、`pad_ms=50`、
`merge_gap_ms=120`、`enter_threshold=0.02`、`exit_threshold=0.05`、
`max_upload_bytes=32MB`、`max_samples_per_job=5_000_000`、
`stream_chunk_samples=4000`。

### 3.3 跑测试

```bash
source .venv/bin/activate
python -m pytest -q
```

测试覆盖（均断言**具体结果或失败类别**，不是“接口能调用”）：

- 阈值附近脉冲：精确区间 `[(155,380)]`，短噪声不割裂、不产生第二区间；
  MID 迟滞带冲掉进入计数的具体位置；
- 跨块长静音：整块 / 逐样本 / 多种乱序切块，`raw` 与最终区间逐值相等；
  carry 游程跨块续满阈值的中间状态；
- 全静音（含短于最小静音）：空结果；
- 末尾未完成段：LOUD 尾、未确认 LOW 尾、MID 尾三种都保留到末尾；
- 边界等号 `|x|==阈值`、合并阈值 `gap==merge_gap` 含等号、保留量裁剪；
- 与**独立标量 oracle**（`tests/conftest.py`，逐样本、不 import 生产内核）
  的随机一致性（5 个参数化种子 + 额外 400 组×8 切块脚本核验）；
- 样本守恒 / 不重叠 / 不越界；
- NaN/Inf → `COMPUTATION_FAILED`；参数/媒体/空输入/超限/状态冲突各自的码与类别；
- 端到端 HTTP：作业状态、幂等重放与冲突、失败作业可查、run 日志可重放。

### 3.4 启动服务并发请求

```bash
python -m uvicorn app.main:app --host 127.0.0.1 --port 8077
```

生成合成夹具：

```bash
python scripts/make_fixtures.py        # 写出 samples/*.wav
```

请求样例（multipart：`file` + JSON 字符串 `config`）：

```bash
curl -s -X POST http://127.0.0.1:8077/jobs \
  -F "file=@samples/long_silence.wav;type=audio/wav" \
  -F 'config={"min_silence_ms":300,"min_activity_ms":100,"pad_ms":50,
              "merge_gap_ms":120,"enter_threshold":0.02,"exit_threshold":0.05}'
```

返回（节选）：

```json
{"job_id":"job-…","run_id":"run-…","status":"SUCCEEDED",
 "sample_rate":1000,"total_samples":800,
 "raw_ranges":[[0,100],[700,800]],
 "intervals":[[0,150],[650,800]]}
```

其余端点：

```bash
curl localhost:8077/jobs                       # 列表
curl localhost:8077/jobs/<job_id>              # 作业详情（FAILED 时带 error 类别）
curl localhost:8077/runs/<run_id>              # 复现追踪（中间状态/事件尾）
curl -X POST localhost:8077/verify -H 'content-type: application/json' \
  -d '{"samples":[0.5,0.5,0,0,0,0,0,0,0.5,0.5],"sample_rate":100,
       "enter_threshold":0.02,"exit_threshold":0.05,
       "min_silence_ms":50,"min_activity_ms":10,"pad_ms":0,
       "merge_gap_ms":0,"chunk_sizes":[1,3,7]}'
```

一键端到端（自动后台拉起服务、发四个夹具、比对写死期望）：

```bash
python scripts/demo_requests.py --serve --port 8077
```

### 3.5 失败如何重放

每次执行在 `data/runs.jsonl` 追加一条，含 `run_id`、作业参数、内部切块大小、
最终状态机快照（state / carry / 进行中计数 / 已出区间）、判定事件尾（含
`silence_confirmed` / `trailing_open` 等理由）、以及失败时的 `failure_category`
与错误详情。原始字节存于 `data/audio/<job_id>.bin`，作业状态在 `data/jobs.db`。
用 `GET /runs/<run_id>` 取条目，结合保存的字节即可按相同参数重跑复现。

---

## 4. 四个规定场景的手工核验（sr=1000，默认参数）

参数：enter=0.02, exit=0.05, min_silence=300, min_activity=100, pad=50, merge_gap=120。

1. **阈值附近脉冲** `threshold_pulse.wav`（1070 样本）：
   `200 低 | 5 中 | 120 响 | 5 中 | 300 低 | 10 响(短噪声) | 430 低`。
   120 LOUD ≥100 退出，区间从 LOUD 起点 205 开始；随后 300 LOW ≥300 在锚点 330
   闭合 → raw `[(205,330)]`；10 样本短噪声 <100 被之后的 LOW 冲掉，不产生区间。
   加保留裁剪 → **`[(155,380)]`**。
2. **跨块长静音** `long_silence.wav`：`100 响 | 600 低 | 100 响`。
   raw `[(0,100),(700,800)]`；间隔 600 远大于合并阈值，保留后
   **`[(0,150),(650,800)]`**；任意切块（含逐样本）结果相同。
3. **全静音** `all_silence.wav`：从未确认活动 → **`[]`**（短于最小静音同样为空）。
4. **末尾未完成段** `trailing.wav`：`200 低 | 100 响 | 100 低`。尾部 100 LOW
   不足 300，不闭合，活动保留到末尾；raw `[(200,400)]` → **`[(150,400)]`**。

## 5. 实测结果

```
89 passed
```

一键演示四个场景全部 `PASS`（输出写入 `samples/demo_result.txt`）；
额外离线核验：400 组随机信号 × 8 种切块与独立 oracle 零分歧；
200 万样本约 0.6s，整段与分块结果一致。
