# Rational-Ratio Polyphase PCM Resampling Service

单声道 PCM 的**有理整数比**多相 FIR 重采样服务。支持分块输入与最终冲刷，
输出样本与切块方式**逐位无关**；抗混叠截止、群延迟、首尾填充全部固定并可审计。

栈：Python 3.10+ / FastAPI / NumPy / SQLite。SciPy **仅**出现在测试中作为独立参考，
生产代码路径不依赖它。

---

## 1. 安装

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt        # 已锁定版本
export PYTHONPATH=src
```

锁定版本（Python 3.12 实测）：numpy 2.5.3、fastapi 0.141.1、uvicorn 0.54.0、
pydantic 2.13.5、scipy 1.18.1（仅测试）、pytest 9.1.1、httpx 0.28.1。

启动服务：

```bash
RESAMPLER_DB_PATH=data/r.db uvicorn resampler.api.app:app \
    --host 127.0.0.1 --port 8080
```

运行测试：

```bash
.venv/bin/python -m pytest tests/ -q
```

## 2. 快速示例

```bash
# 查看 48 kHz -> 16 kHz 的滤波器契约（截止、延迟、填充）
curl -s -X POST localhost:8080/resample/validate \
    -H 'Content-Type: application/json' \
    -d '{"input_rate":48000,"output_rate":16000}' | python -m json.tool

# 建作业
J=$(curl -s -X POST localhost:8080/jobs -H 'Content-Type: application/json' \
    -d '{"input_rate":48000,"output_rate":16000,"input_format":"f64le",
         "output_format":"f64le"}' | python -c 'import sys,json;print(json.load(sys.stdin)["job_id"])')

# 任意切块追加原始 little-endian double PCM
curl -s -X POST localhost:8080/jobs/$J/chunks --data-binary @part0.f64
curl -s -X POST localhost:8080/jobs/$J/chunks --data-binary @part1.f64

# 冲刷尾部并取回完整结果
curl -s -X POST localhost:8080/jobs/$J/flush
curl -s localhost:8080/jobs/$J/result -o out16k.f64        # 原始 f64le
curl -s "localhost:8080/jobs/$J/result?format=json"        # 或 base64 JSON
```

CLI（WAV 与 raw，自动分块）：

```bash
python -m resampler.cli validate --input-rate 48000 --output-rate 16000
python -m resampler.cli resample-wav --input in48k.wav \
    --input-rate 48000 --output-rate 16000 --output out16k.wav
python -m resampler.cli resample-raw --input in.f64 --input-rate 8000 \
    --output-rate 12000 --chunk-samples 37 --output out.f64
```

## 3. HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查 |
| POST | `/resample/validate` | 归约 L/M，返回抽头数、通带/阻带/截止频率、群延迟、填充策略 |
| POST | `/jobs` | 建作业（速率、PCM 格式、容器、溢出策略、可选滤波器参数） |
| POST | `/jobs/{id}/chunks` | 追加一块：`application/octet-stream` 原始字节，或 JSON `{"data_b64": ...}` |
| POST | `/jobs/{id}/flush` | 释放 K−1 个零填充的尾部输出，作业进入 `flushed` |
| GET | `/jobs/{id}` | 状态与计数器（含失败原因） |
| GET | `/jobs/{id}/result` | 完整结果；默认原始 PCM/WAV 字节，`?format=json` 为 base64；响应头带群延迟 |

作业状态机：`open -> flushed`（正常终态）或任意阶段 `-> failed`（终态）。
向 `flushed/failed` 作业追加数据或二次冲刷均为 **409**。

支持的 PCM 格式：`u8 s16le s24le s32le f32le f64le`；容器：`raw` 或 `wav`
（WAV 作业一次性提交完整文件；仅单声道 PCM/IEEE-float，压缩格式拒绝）。

## 4. 错误契约（五类，可区分）

| category | HTTP | code | 触发示例 |
|---|---|---|---|
| `input_error` | 400 | `invalid_input` | 速率非正/非整数、字节未按采样宽对齐、WAV 多声道或压缩、输入含 NaN/Inf、未知 job |
| `state_conflict` | 409 | `state_conflict` | flush 后再写、二次 flush、未 flush 取结果、作业已 failed |
| `resource_exhausted` | 413 | `resource_exhausted` | 单块/累计样本超限、作业数超限、滤波器抽头超限、L/M 项超限 |
| `output_overflow` | 422 | `output_overflow` | `clip_policy="reject"` 下整数编码越界；float 转换溢出；对非有限值编码 |
| `computation_failure` | 500 | `computation_failure` | 内核算术产生非有限值、冲刷后样本计数与计划不符 |

响应体统一为：

```json
{"error": {"category": "...", "code": "...", "message": "...",
           "detail": {"job_id": "...", "chunk_index": 3, "...": "..."}}}
```

失败会把作业置为 `failed` 并在 SQLite 中持久化 category/code/message，便于复盘。

## 5. 关键数值契约（详见 [docs/DESIGN.md](docs/DESIGN.md)）

* 比例仅接受**整数采样率**，归约为互质 `L/M`（f_out/f_in = L/M）。
* 阻带边 = 新/公共 Nyquist = `min(f_in,f_out)/2`；通带边默认 `0.9 ×` 阻带边；
  截止取过渡带中点；Kaiser 窗默认 80 dB 衰减（β=0.1102(A−8.7)）。
* 原型长度 `N = K·L`，K 为偶数；全局归一化使 `sum(h)=L`（系统 DC 增益恰为 1）。
* 输出 n 对齐输入时间 `t(n) = (n·M − (K−1)/2)/(L·f_in)`，
  群延迟 `(K−1)/2` 个输入样本。
* **固定填充**：输入端补 K−1 个引导零，flush 时补 K−1 个尾随零。
* 输出总数（仅取决于输入样本数 J，与切块无关）：
  `J=0 → 0`；`J≥1 → ceil(L·(J+K−1)/M)`。
* 非有限输入：媒体边界即拒绝（400）；内核自卫性检测（500）。
* 整数输出默认 `clip`（饱和并计数，响应头 `X-Clipped-Samples`），
  可选 `reject`（422，作业失败）。

## 6. 测试与可复核日志

`tests/` 七组共 80 个断言型测试：滤波器设计具体数值、脉冲与 **SciPy `upfirdn`
及自写 FFT 参考**逐样本对照（≤ 4e-14）、低频正弦保真、超新 Nyquist 混叠抑制、
上采样镜像抑制、12 种切块逐位相等、极短/空流、全 PCM/WAV 编解码、五类错误
及 HTTP 状态码。参考信号**不**由被测内核生成。

每次请求/测试都有 `run_id`（可用 `X-Run-Id` 头指定），落盘到

```
logs/runs/<run_id>/events.jsonl    # 事件流：创建、每块前后内核状态、失败分类
logs/runs/<run_id>/summary.json    # 通过/失败断言计数与名称
```

内核快照包含 `inputs_seen / outputs_emitted / next_output_index / hist 头尾 /
flushed`，足以重放切块相关问题。

## 7. 已知限制

* 只支持单声道、精确整数比；非整数有理比（如 48000.5）按契约拒绝。
* 内存中保留每作业浮点输出（SQLite BLOB），适合中小规模流；超大流需外置对象存储。
* WAV 输入按整文件提交（流式 WAV 解析超出范围）；raw PCM 可任意分块。
* 无鉴权/多租户；仅供本地/内网复核使用。
