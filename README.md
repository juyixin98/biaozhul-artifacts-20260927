# resamp — 单声道 PCM 有理比率多相 FIR 重采样服务

一个可审查的音频重采样实现：把单声道 PCM 以**约简为整数比** `fout/fin = L/M`
进行多相（polyphase）FIR 重采样，支持**分块推送**与**最终冲刷（flush）**，
分块方式不改变有效输出与样本计数（逐位一致）。技术栈：Python 3.10+、
FastAPI、NumPy、SQLite（无 scipy 依赖；FIR 与 I0 均自包含）。

> 数据与外部参与者全部是本地合成夹具，无需任何生产账号或真实业务数据。

---

## 1. 快速开始

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -e .

# 运行测试（会在 tests/_runs/<run_id>/run.log.jsonl 留下可重放日志）
python -m pytest -q

# 启动服务
uvicorn resamp.api.app:app --host 127.0.0.1 --port 8000

# 另开一个终端：HTTP 端到端示例
python examples/client_demo.py --base-url http://127.0.0.1:8000

# 命令行离线 WAV 重采样（不起服务）
python examples/resample_wav.py in.wav out.wav 48000 --chunk 1024
```

依赖已在 [`requirements.txt`](requirements.txt) 中锁定直接版本；完整传递依赖
（含哈希来源版本）冻结在 [`requirements.lock`](requirements.lock)，可用
`pip install -r requirements.lock` 复现完全一致的环境。

---

## 2. 工程边界与模块划分

```
src/resamp/
  config.py            # 设置（TOML + RESAMP_* 环境变量）
  errors.py            # 四类错误契约（见 §6）
  media.py             # 媒体解析边界：单声道 16/24/32-bit PCM WAV <-> float64
  storage.py           # 作业状态：SQLite 元数据 + 分块审计行 + 输出 .f64 文件
  service.py           # 作业编排：状态机、资源限额、载荷编解码
  dsp/
    ratios.py          # fin/fout -> 互质 L/M（含上限保护）
    fir.py             # Kaiser 加窗 sinc 原型低通（自包含，无 scipy）
    polyphase.py       # 流式多相重采样核心（核心审查对象）
    reference.py       # 独立高精度离线参考：显式零填充 + FFT 全卷积
  api/
    schemas.py         # pydantic 请求/响应模型
    routes.py          # /v1/design, /v1/jobs..., /v1/validate, /v1/resample...
    app.py             # 应用工厂、请求 id 中间件、错误映射、生命周期
examples/              # HTTP 客户端演示、离线 WAV CLI
tests/                 # 独立测试（见 §7），含可重放运行日志
```

模块间只通过明确的数据/错误类型通信：DSP 层只接受/返回 1-D `float64`
NumPy 数组并抛出 `resamp.errors.*`；服务层把同样的错误透传给 HTTP；HTTP 层
**只做状态码映射，不发明错误**。

---

## 3. DSP 契约（审查要点）

完整推导见 [`docs/design.md`](docs/design.md)，此处给出可直接核对的不变量。

设 `L`（上采样）、`M`（下采样）互质，原型滤波器 `h` 长 `2H+1`、
在零填充高采样率流上的直流增益为 `L`：

```
xz[k] = x[k/L]   当 k % L == 0，否则 0
y[n]  = Σ_j x[ floor(nM/L) - j ] · h[ (nM mod L) + j·L ]
```

* **整数比**：`L = fout/gcd(fin,fout)`，`M = fin/gcd(...)`。
* **抗混叠截止**：原型截止 `fc = min(fin,fout)/2`；过渡带以 fc 为中心，
  通带边 `fpass=(1−w)fc`、阻带边 `fstop=(1+w)fc`，默认 `w=0.1`，Kaiser
  阻带衰减 80 dB（实测各比例阻带约 −79 dB、通带纹波 ≈0.002 dB）。
* **滤波器延迟**：群延迟 `H` 个高采样率样本 = `H/L` 输入样本 = `H/M`
  输出样本 = `H/(L·fin)` 秒；`/v1/design` 与作业元数据中均显式给出。
* **首尾填充（固定）**：
  * 头部固定补 `P = H // L` 个零，使输出与输入**时间对齐**：流中部的脉冲
    在输出 `floor(L·p/M)` 处出现峰值；
  * `flush()` 时尾部固定补 `Z = 2H//L + 2` 个零，必然覆盖最长向后抽头，
    然后输出全卷积意义上的全部剩余样本。
  * 不做淡入/淡出；首尾瞬态就是零填充对称 FIR 的瞬态。
* **确定性样本计数**（`N_in ≥ 1`）：

  ```
  N_out = floor((L·(N_in − 1) + H) / M) + 1
  ```

  `N_in = 0` 时输出 0。该计数与具体分块无关。
* **跨块相位保持 / 分块一致性**：每个输出都用固定的一维 `np.dot` 在固定
  臂上累加（不随行数切换 BLAS 归约路径），因此**不同切块产出逐位相同**
  （测试用 `np.array_equal` 断言，而非容差）。

### 独立参考

`dsp/reference.py` **不复用多相核心的算法路径**：显式构造长度 `L·N` 的
零填充序列，做一次长度为 2/3/5-smooth 的零填充循环 FFT 全卷积，再按 `nM`
取点。它与多相核心只共享“滤波器系数设计”（设计表等价物），算术路径独立。
核心在所有比例上与参考的内部样本最大误差约 `1e-15`（浮点舍入量级）。

---

## 4. HTTP 接口

所有错误响应都带 `request_id`（也回写到响应头 `x-request-id`）。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET  | `/health` | 存活检查 |
| POST | `/v1/design` | 校验采样率对，返回 L/M、抽头数、截止/通阻带、群延迟、首尾填充、计数示例 |
| POST | `/v1/jobs` | 创建作业 `{fin,fout,output_dtype}` |
| POST | `/v1/jobs/{id}/push` | 推送一块样本（见载荷格式） |
| POST | `/v1/jobs/{id}/flush` | 冲刷尾部、作业进入 `completed` |
| GET  | `/v1/jobs/{id}` | 作业状态与累计计数 |
| GET  | `/v1/jobs/{id}/chunks` | 每块 `n_in/n_out/kind` 审计行（用于重放） |
| GET  | `/v1/jobs/{id}/result` | 全部输出（base64 float64-LE） |
| POST | `/v1/validate/payload` | 只做边界校验：解码、有限性、计数、min/max、时长 |
| POST | `/v1/resample/wav` | 一次性：base64 单声道 PCM WAV → 重采样 WAV |
| POST | `/v1/resample/samples` | 一次性：base64/json 样本 → 重采样样本 |

样本载荷：

```json
{ "encoding": "base64-float64-le", "data": "<base64 of little-endian float64>" }
```

或（小数据量，受 `json_list_sample_cap` 限制）：

```json
{ "encoding": "json", "data": [0.0, 0.1, -0.2] }
```

### curl 小例

```bash
curl -s -X POST localhost:8000/v1/design \
  -H 'content-type: application/json' \
  -d '{"fin":8000,"fout":48000}'
```

---

## 5. 作业状态机

```
created ──push──▶ running ──push...──▶ running ──flush──▶ completed
   │                 │
   └──── 任何处理失败 ────────────────────────────────▶ failed
```

* `created/running` 才能 push/flush；对 `completed` 再 push/flush → 409。
* 未知作业 → 404。
* 每次 push/flush 都在 SQLite `chunks` 表记录 `seq, kind, n_in, n_out, ts`，
  输出样本追加到 `data/jobs/<id>.out.f64`，无需把大数组放进数据库行。

---

## 6. 错误契约（四类必须可区分）

| 类别 `category` | 错误码 | HTTP | 触发示例 |
|---|---|---|---|
| `invalid_input` | `invalid_input` / `not_found` | 400 / 404 | NaN/Inf 样本、非单声道、非 PCM、坏 base64、未知作业 |
| `state_conflict` | `state_conflict` | 409 | completed 后再 flush/push |
| `resource_exhausted` | `resource_exhausted` | 413 | 单块超限、作业总输入超限、滤波器抽头超上限、L/M 因子超上限 |
| `computation` | `computation_failed` | 422 | 累加产生 NaN/Inf、float32 输出溢出 |

响应体形如：

```json
{ "error":"invalid_input", "category":"invalid_input",
  "message":"input contains non-finite sample",
  "details":{"index":4,"value":"non-finite:inf"},
  "request_id":"..." }
```

> NaN/Inf 不是合法 JSON 数值，因此非有限样本走二进制 `base64-float64-le`
> 载荷；错误详情中的非有限值会被序列化为 `"non-finite:<x>"` 字符串。

---

## 7. 测试与可重放日志

```
tests/
  conftest.py             # run_id 生成 + JSONL 记录器
  test_ratio_fir.py       # 整数比、Kaiser β/长度、独立闭式 sinc+窗交叉核对、频响
  test_kernel.py          # 脉冲响应、多相臂划分、计数公式、跨块相位、群延迟
  test_signals.py         # 低频正弦保真、超新 Nyquist 抑制、朴素降采样对照、镜像
  test_short_chunks.py    # 空流/单样本/极短流、多种切块逐位一致、空块
  test_errors.py          # 四类失败各自的错误类别
  test_service.py         # SQLite 计数、状态机、413/409/404、载荷编解码
  test_media.py           # 16/24/32-bit PCM 往返、立体声/8-bit/垃圾拒绝
  test_api.py             # 真实 FastAPI + httpx 端到端
```

测试不是“接口能调用”式的：它们断言具体数值与**失败类别**，例如

* 通带幅度（平顶窗测幅，误差 < 0.2%）、频率落点、SNR > 70 dB；
* 超新 Nyquist 频率的混叠/镜像电平 **< −55 dB**；
* 一个**朴素降采样对照实验**必须检出强混叠（证明测试本身有效）；
* 分块输出 `np.array_equal`（逐位）且与独立 FFT 参考误差 < 1e-9；
* 极短流（0/1/2/3/… 样本）的精确输出计数。

每次 `pytest` 会话生成一个 `run_id`（UTC 时间戳+短 uuid，可用环境变量
`RESAMP_TEST_RUN_ID` 固定），关键中间状态、阈值、形状、判定理由写入
`tests/_runs/<run_id>/run.log.jsonl`，结束时终端摘要打印该路径。

---

## 8. 配置

复制 [`config.example.toml`](config.example.toml)，用环境变量指定：

```bash
export RESAMP_CONFIG=config.example.toml
# 也可逐项覆盖：RESAMP_MAX_TOTAL_INPUT_SAMPLES、RESAMP_ATTENUATION_DB ...
uvicorn resamp.api.app:app
```

关键项：采样率上下限、`max_ratio_factor`（L/M 上限）、单块/总样本上限、
`max_filter_taps`（抽头上限）、Kaiser 衰减与过渡带比例、json 列表样本上限。

---

## 9. 已知限制（如实说明）

* **仅单声道 PCM WAV**（16/24/32-bit，format tag 1）。不支持立体声/多声道、
  μ-law/A-law、浮点 WAV、MP3/AAC/FLAC/容器格式；如需要应在 `media.py`
  边界内扩展，DSP 核心保持不变。
* **有理比率必须约简到 L/M**；当两个采样率近似无关（互质因子超过
  `max_ratio_factor`）时返回 `resource_exhausted`，而不是隐式近似比例。
  这是有意为之，避免在审查中掩盖比例失配。
* 输出点积是逐输出的一维 `np.dot`（为逐位确定性而放弃了批量 GEMV 的速度）。
  音频规模（几万~几十万样本、几十抽头）下已验证可用；超大批量的吞吐不是
  本实现的优化目标。
* 服务进程在内存中持有活动作业的重采样器对象；SQLite/输出文件是审计与
  结果的权威持久层，但**跨进程续传未实现**（重启后对历史作业继续 push 会
  返回状态冲突，结果仍可读）。
* 并发使用单进程 + 服务级 RLock；SQLite 为 WAL，未做水平扩展。
* 时间基准假定均匀采样、无时钟漂移；不做去加重/抖动/噪声整形。
