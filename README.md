# 离线 RTP 抖动缓冲与播放计划后端

本地实现的 **RTP 音频包离线抖动缓冲（jitter buffer）与播放计划（playout schedule）** 后端。
全部数据来自本地确定性合成夹具，不依赖生产账号、真实业务数据或外部参与者。

- **语言/技术栈**：Python 3.10+、FastAPI、NumPy、SQLite（标准库）、pytest
- **性质**：离线事件驱动仿真。输入是“抓包轨迹”（RTP 报文 + 到达时刻），
  输出是逐帧播放计划、显式空缺、分类丢弃原因，并由**独立参考判定器（oracle）**复核。

---

## 1. 它实现了什么（行为契约）

1. **序号 / 时间戳按各自位宽展开；SSRC 变更新建会话**
   - 16 位序号、32 位 RTP 时间戳由两个独立的回绕展开器处理
     （`app/timekit/unwrap.py`），正向回绕计数、半环反向跳变单独计数。
   - 仿真器按 SSRC 路由：新 SSRC 出现即创建独立 `SessionPlanner`，
     序号空间互不污染（见 `ssrc_switch` 场景）。

2. **延迟包与重复包分别处理；播放期限过后不能插回**
   - `duplicate`：同一 `(ssrc, 展开序号)` 再次到达（时间戳一致）。
   - `late_after_playout`：该序号位置已结算（音频或空缺）后才到，
     **绝不插回**，且该位置保留原结论。
   - 迟到判定基于“**包自身到达时刻 vs 它的槽播放期限**”，与泵在稍后哪个
     tick 处理无关——期限前到达的包永远是真实音频。
   - 另区分 `overflow`（缓冲硬上界尾丢弃）、`ssrc_conflict`、
     `ts_regression`（同序号时间戳不一致）、`parse_error`。

3. **自适应延迟公式与上下限明确**
   - RFC 3550 A.8 抖动 EWMA：`J += α(|D| − J)`，`α = 1/16`。
   - 排队峰值 `spike = max(transit − min_transit)`：捕获 EWMA 平滑掉的突发。
   - 目标延迟（自适应臂）：

     ```
     need  = max(k·J, 话峰内 spike, 跨话峰峰值记忆) + 漂移裕度
     D     = persist·D_prev + (1−persist)·need      # 跨话峰慢放
     target= clamp(max(need, D), min_delay_us, max_delay_us)
     ```

     默认 `k=4`，`[20ms, 400ms]`，`persist=0.75`。
   - 话峰进行中 spike 抬升时，前沿对**尚未排程**的后续槽即时上调
     （只升不降）；已排程槽不回拨。
   - **固定延迟基线**：经典静态播放网格（锚点 + 恒定标称帧长），
     不跟随到达/漂移，用于与自适应对比。

4. **缺包用显式空缺标记，绝不生成虚假音频**
   - 缺失位置输出 `kind=gap` 帧：`payload=None`、`arrival_us=None`、
     `rtp_ts=None`，带 `gap_reason=missed_at_deadline`。
   - 合成 PCM 负载由 `(ssrc, 展开序号, 展开时间戳)` 决定，
     oracle 逐字节校验每个音频帧，因此空缺无法被“猜”出来。

---

## 2. 模块组织（多模块、职责分离）

```
app/
├── media/                 媒体解析层
│   ├── rtp.py             RFC 3550 RTP 线格式解析/构造（严格错误码）
│   └── pcm.py             本地确定性 PCM16 合成与逐字节校验
├── timekit/               时间与信号内核
│   ├── unwrap.py          16/32 位回绕展开器（各自独立）
│   └── clock.py           RFC3550 抖动 EWMA + 排队峰值 + 收发时钟漂移比
├── core/                  核心机制
│   ├── models.py          Frame / Gap / Drop 原因枚举（错误语义唯一真源）
│   ├── planner.py         单 SSRC 抖动缓冲 + 播放计划
│   ├── simulator.py       离线事件循环（按 SSRC 路由、播放时钟 tick）
│   ├── fixtures.py        合成夹具：突发乱序/漂移/回绕/暂停重启/重复/溢出
│   ├── oracle.py          独立参考判定器（不 import planner）
│   ├── scenarios.py       7 个验证场景 + 具体断言 + 双臂对比
│   └── pipeline.py        HTTP 输入 → 双臂结果/oracle/错误归类
├── jobs/                  作业状态
│   ├── store.py           SQLite 持久化（queued→running→succeeded/failed）
│   └── runner.py          单线程后台执行器
├── api/                   验证接口
│   ├── schemas.py         Pydantic 模型
│   └── app.py             FastAPI：健康/场景/原始计划/异步作业
├── config.py              全部算法参数（env 可覆盖）
├── logging_setup.py       结构化 JSON 日志（request_id / job_id 关联）
└── main.py                ASGI 入口
tests/                     独立 pytest（41 个，断言具体数字与失败类别）
scripts/demo.py            命令行跑场景；scripts/smoke_api.py 接口冒烟
```

**核心机制不被硬编码演示替代**：演示只是 `core/` 的一层薄封装；
oracle 刻意不 import `app.core.planner`，参考答案不是被测核心自己生成的。

---

## 3. 安装与运行

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 跑测试（实际执行并报告结果）
python -m pytest tests/ -q

# 命令行演示：7 个夹具场景 + 双臂对比 + 断言明细
python scripts/demo.py

# 接口端到端冒烟（进程内，不绑端口）
python scripts/smoke_api.py

# 启动 HTTP 服务
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

服务入口：`app.main:app`（依赖与版本见 `requirements.txt`）。

---

## 4. HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET  | `/health` | 状态、版本、模块位置、可用场景 |
| GET  | `/scenarios` | 场景清单 |
| POST | `/validate/scenario` | 同步跑一个内置夹具场景 |
| POST | `/validate/all` | 同步跑全部场景，聚合 passed |
| POST | `/validate/plan` | 提交原始 RTP 报文（base64/hex + 到达时刻）跑双臂 |
| POST | `/jobs/scenario` | 异步提交场景作业（202 + job_id） |
| POST | `/jobs/raw` | 异步提交原始报文作业 |
| GET  | `/jobs` / `/jobs/{id}` | 作业列表 / 作业详情（含结果） |

每个响应都带 `X-Request-ID`（可用请求头 `X-Request-ID` 指定）与
`X-Service-Version`，日志以同一 `request_id`/`job_id` 关联。

### 错误语义（稳定 `error_code`，不伪装成功）

| HTTP | error_code | 含义 |
| --- | --- | --- |
| 400 | `bad_packet` / `bad_encoding` / `missing_field` | 原始报文/编码问题 |
| 404 | `unknown_scenario` / `not_found` | 场景或作业不存在 |
| 422 | （Pydantic） | 请求体字段校验失败 |
| 500 | `internal_error` | 未预期异常 |

注意：**RTP 解析失败不是 4xx/500**，而是被计入结果的 `parse_errors`
（丢弃原因 `parse_error`，带 `truncated/bad_version/bad_extension/bad_padding`
等子原因），因为一批轨迹里混入坏包是正常的验证输入。
“不确定”结论（如漂移样本不足）单列在 `uncertain` 字段，与 pass/fail 分开。

快速试一条：

```bash
curl -s -X POST localhost:8000/validate/scenario \
  -H 'content-type: application/json' \
  -d '{"scenario":"burst_reorder"}' | python -m json.tool
```

---

## 5. 验证夹具与“具体结果”（复现步骤）

| 场景 | 夹具 | 断言的具体结果 |
| --- | --- | --- |
| `burst_reorder` | 每话峰早段窗口整体延后 120ms | 自适应仅冷启动话峰丢 1，学到目标延迟 ≥100ms 后后续话峰 0 缺失；固定 40ms 每话峰都丢（≥3） |
| `clock_drift` | 发送端快 2%，3×200 包长话峰 | 自适应漂移比收敛到 1.02±0.004、0 缺失；固定静态网格缺失上百 |
| `wraparound` | 序号从 65530、ts 从 0xFFFFFF00 起 | 序号/时间戳各正向回绕 ≥1 次，回绕后 80 帧连续、负载逐字节一致 |
| `pause_resume` | 第 100 包后发送端停发 1.5s（序号与时间戳一并跳过静默帧，期间只推进播放时钟 tick） | 恰好 75 个显式空缺、200 真实音频（无合成补帧），静默时长 1500ms 单列 |
| `duplicates` | 3 个包延迟重发 | `duplicate==3`、0 空缺、120 真实音频 |
| `overflow` | 同刻 300 包、缓冲上界 250 | `overflow==50`、峰值缓冲 ≤250、音频 250 + 空缺 50 = 300 |
| `ssrc_switch` | 两 SSRC 交错 | 恰好 2 个独立会话、160 音频、0 串话 |

每个场景同时跑 **自适应** 与 **固定 40ms 基线**，并对两臂都跑 oracle：

- `PLAYOUT_MONOTONIC`：每 SSRC 播放时间严格单调；
- `SEQ_CONTIGUOUS` / `GAP_PAYLOAD_NONE`：序号连续无重复、空缺无负载；
- `BOUND_TARGET_DELAY` / `BOUNDED_OCCUPANCY`：目标延迟与缓冲占用有界；
- `AUDIO_CONTENT_MATCH`：音频负载与合成夹具逐字节一致；
- `DUPLICATES_RECORDED` / `NO_LATE_REINSERT` / `DROP_REASONS_STABLE` /
  `SSRC_SEPARATION`：丢弃分类与不可插回。

---

## 6. 复现一次完整验收

```bash
source .venv/bin/activate
python -m pytest tests/ -q        # 41 passed
python scripts/demo.py            # 7 场景全 PASS，打印具体数字
python scripts/smoke_api.py       # HTTP 全端点 200/202/404 正确
```

SQLite 默认落在 `data/jitter.db`（可用 `JITTER_DB_PATH` 覆盖）；
日志默认 JSON 到 stderr（`JITTER_LOG_JSON=0` 切人类可读，
`JITTER_LOG_LEVEL=DEBUG` 提级）。

## 7. 关键设计取舍

- **离线事件循环 + 显式播放时钟 tick**：暂停期间没有包到达，但播放时钟
  仍逐帧前进，因此缺失位置能被如实地标记为空缺，而不是等下一个包“补推”。
- **迟到包仍参与抖动/峰值统计**：否则系统永远学不到导致丢包的那次突发。
- **固定基线用静态网格而非到达驱动**：若固定臂也用 `max(到达+delay, 上一槽)`，
  它会静默吸收任意漂移，对比失去意义；静态网格才会真实暴露固定延迟的代价。
- **漂移只用接近标称间隔（±10%）的连续包估计**：突发乱序的异常间隔不污染时钟比。
