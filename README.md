# Subtitle Guard — SRT/WebVTT 字幕校验与建议修复后端

纯后端服务：对 **SRT** 与 **WebVTT 受限子集** 字幕做严格校验，诊断具体问题类别，
并在“最小时间位移 + 最大改动预算”约束下给出**建议修复方案**。无法满足约束时
**不会强行挤压或删除任何字幕**。

技术栈：Python 3.11+ · FastAPI · NumPy · SQLite（标准库）· pytest。
全部输入均为本地合成夹具，无任何外部账号或真实业务数据依赖。

---

## 1. 快速开始

```bash
# 1) 虚拟环境与依赖
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 2) 运行测试（实际执行并报告结果，约 20 秒）
.venv/bin/python -m pytest tests/ -q

# 3) 本地演示脚本（逐个跑合成夹具，打印判定与修复步骤）
.venv/bin/python scripts/demo.py --quiet-logs

# 4) 启动 HTTP 服务
.venv/bin/python -m app.main
#   或： .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
```

冒烟（服务启动后另开终端）：

```bash
curl -s http://127.0.0.1:8000/health | python3 -m json.tool
curl -s -X POST http://127.0.0.1:8000/api/v1/validate \
  -H 'Content-Type: application/json' \
  -d '{"content":"1\n00:00:01,000 --> 00:00:03,000\nhi\n"}' | python3 -m json.tool
```

也可以直接用 uvicorn 的自动重载进行开发：`.venv/bin/uvicorn app.main:app --reload`。

---

## 2. 工程结构（按职责分层，非单文件/非调用壳）

```
app/
  config.py              配置层：阈值、段边界、预算，均可用环境变量覆盖
  logging_setup.py       带运行身份(run/job id)的结构化日志
  core/                  时间与信号内核
    timeutil.py          严格时间戳解析/格式化（整数毫秒）
    markup.py            受限子集内联标记检查（文本不重写，只诊断）
    diagnosis.py         NumPy 向量化诊断（负/零/过短/过长、重叠、同起点、越界）
    solver.py            最小位移修复求解（加权 L1 保序 DP，NumPy 加速）
    models.py            Cue / Diagnostic / RepairPlan 等领域模型
  media/                 媒体解析层
    parser.py            SRT / WebVTT 严格解析器（UTF-8、BOM、CRLF）
    writer.py            重新序列化（原文与标记逐字保留，字幕一条不丢）
  services/
    pipeline.py          编排：parse → diagnose → solve → render
  jobs/                  作业状态层
    store.py             SQLite 持久化（显式状态机，含 error 终态）
    service.py           作业执行（异常绝不伪装为成功）
  api/
    schemas.py           Pydantic 请求/响应模型
    app.py               FastAPI 路由与错误信封
  main.py                可运行服务入口
scripts/demo.py          本地端到端演示
tests/
  fixtures/              本地合成夹具（见第 6 节）
  oracle.py              独立参考实现（DFS 穷举 + 独立 numpy DP），不导入被测求解器
  test_*.py              断言具体数值与失败类别
  logs/                  每次 pytest 会话一份可关联运行身份的日志
```

---

## 3. 核心算法与状态处理

### 3.1 严格时间解析与文本保留
* 全程**整数毫秒**运算，杜绝浮点漂移。
* SRT 仅接受 `HH:MM:SS,mmm`（逗号、三位毫秒、分秒 00–59）；
  VTT 接受 `HH:MM:SS.mmm` / `MM:SS.mmm` / `SS.mmm`（点号）。
  `60` 秒、两位毫秒、错误分隔符等一律拒绝并给出 `invalid_timestamp`。
* 文本按 UTF-8 严格解码（坏字节 → `encoding_error`），BOM 记录但不乱丢，
  CRLF 规范化为 LF；**正文行与内联标记逐字保留**，实体（`&amp;`、`&#128512;`）
  不解码。修复输出中每个 cue 的正文与输入完全一致。

### 3.2 诊断（各自独立的失败码，绝不笼统“成功/失败”）
| code | 严重度 | 含义 |
|---|---|---|
| `negative_duration` | error | end 早于 start（修复时翻转 start/end 并记录原因） |
| `zero_duration` / `too_short` / `too_long` | warning | 时长异常 |
| `overlap` | warning | 相邻 cue（按起点排序）时间重叠，含连锁 A>B>C 的每一对 |
| `same_start` | warning | 两条 cue 起点完全相同 |
| `crosses_boundary` | warning | 跨过段边界（默认 30s/60s） |
| `starts_before_zero` / `ends_past_horizon` | error | 越过内容范围 |
| `invalid_timestamp` `bad_timing_line` `missing_timing` `invalid_header` | error | 结构错误 |
| `srt_positioning_not_supported` `vtt_settings_not_supported` `vtt_block_not_supported` | error | 子集外的定位/设置/STYLE/REGION |
| `unsupported_markup` `unpaired_tag` | error | 子集外标记（`<c> <v> <ruby> 内联时间戳` 等）或配对错误 |
| `encoding_error` | error | 非 UTF-8 字节 |

### 3.3 修复求解：最小位移、受预算约束、无解不强压
只允许**平移时间**（负时长先翻转、过短/零时长只延长、**从不压缩**过长 cue）：

```
minimize   Σ |s_i − 归一化原起点_i|
s.t.       s_{i+1} ≥ s_i + d_i + G            # G=最小间隔，消除重叠
           每条 cue 留在其当前所在段内          # 不允许借“跨段”化解冲突
           |s_i − 归一化原起点_i| ≤ 单 cue 上限
           d_i ≥ max(原时长, 最短显示时长)       # 仅延长，不挤压
```

经变量代换 `y_i = s_i − Σ_{j<i}(d_j+G)` 变为**带逐点盒约束的加权 L1 保序回归**，
用精确动态规划在候选值集合上求解（NumPy 向量化松弛，O(n·m) 时间），
再做“能不动就不动、扰动向下游传播”的最优重建。每个 cue 记录
`action ∈ {held, moved, flipped, extended, flipped+moved, extended+moved}`
与 `reasons`（诊断码；纯被上游挤动记 `chain_propagation`）。

三种非成功终态明确区分，且**都不产出修复文档**：

* `infeasible_bounds`：段/单 cue 位移上限下数学上不可行；
* `budget_exceeded`：可行，但最小总位移超过总预算（响应消息给出所需最小位移）；
* `solver_too_large`：超过精确 DP 的内存护栏（提示拆分作业），而非降级乱算。

原文、原始起止时间与修复原因全部保留在响应里。

### 3.4 作业状态机（SQLite）
`queued → running → succeeded | failed | parse_failed`，
未捕获异常进入 `error`（落库堆栈消息），**未知状态不会被映射为成功**。
输入存为 BLOB + SHA-256；列表接口不回显正文。

---

## 4. HTTP 接口与错误语义

### 文档级结果不是 HTTP 错误
输入文档本身的问题（解析失败、不可修复、超预算）一律 **HTTP 200**，
通过响应体的 `status` 与 `failure_codes` 表达；HTTP 状态码只表示协议/传输问题：

| HTTP | error_code | 触发条件 |
|---|---|---|
| 400 | `INVALID_REQUEST` | content 为空 |
| 404 | `JOB_NOT_FOUND` | 作业不存在 |
| 413 | `PAYLOAD_TOO_LARGE` | 超过 8 MiB |
| 422 | （FastAPI 校验） | 请求体不符合 schema（如 `format:"pdf"`） |
| 500 | `INTERNAL_ERROR` | 未预期异常 |

错误信封：`{"error_code", "message", "detail"}`。

### 端点
* `GET  /health` — 服务状态与版本（Python/NumPy/FastAPI/pydantic/SQLite）和当前配置。
* `POST /api/v1/validate` — 同步校验/修复。
  请求 `{"content": "<UTF-8 文本>", "format": "srt|vtt|null"}`；
  响应含 `run_id, status, cue_count, diagnostics[], repair, repaired_document,
  failure_codes, elapsed_ms, message`。
* `POST /api/v1/jobs` — 创建持久化作业（同步执行完毕返回 201 + job url）。
* `GET  /api/v1/jobs?limit=` — 作业列表（不含正文）。
* `GET  /api/v1/jobs/{job_id}` — 作业详情（含结果与修复文档）。

`status` 取值：`clean | repaired | parse_failed | infeasible_bounds |
budget_exceeded | solver_too_large`。

### 配置（环境变量前缀 `SUBGUARD_`）
`MIN_DURATION_MS=1000`、`MAX_DURATION_MS=7000`、`MIN_GAP_MS=1`、
`SEGMENT_BOUNDARIES_MS=30000,60000`、`HORIZON_MS=90000`、
`MAX_PER_CUE_SHIFT_MS=5000`、`MAX_TOTAL_SHIFT_MS=60000`、
`MAX_CUES=10000`、`DB_PATH=data/subguard.db`、`LOG_LEVEL=INFO`。

---

## 5. 可复核性：独立参考实现与穷举验证

`tests/oracle.py` 是**从零写的独立参考实现，不 import 被测求解器**：

* `oracle_min_shift`：在候选网格上做带界 DFS **穷举全部可行摆放**，返回真实最小
  位移与 argmin（n 小、窗口紧凑时使用）；
* `oracle_min_shift_dp`：独立的密集整数毫秒 NumPy DP（不做候选压缩，实现形态与
  生产求解器不同），用于 1ms 间隔等非格点随机实例。

`tests/test_exhaustive.py` 覆盖约 **9,900** 个穷举/对拍用例：

* n=1/n=2 在 250ms 格点上的全笛卡尔网格（正常盒约束）；
* 小段+小位移上限的紧约束扫描（大量 `infeasible_bounds` 判定对拍）；
* n=3 小窗口稠密格点扫描；
* 370 个 gap=1ms 的随机实例（目标在格点、最优可偏离格点），与独立 DP 对拍；
* 每个修复结果在测试内**独立复核全部约束**（间隔、段边界、单 cue 上限、
  时长未被压缩、目标值等于申报值），不仅检查“接口能调用”。

其余测试断言具体数值与失败类别，例如：
* 两 cue 冲突 499ms → 最优总位移恰为 501ms、早 cue 锚定；
* 连锁重叠夹具修复后每对相邻 cue 间隔 ≥1、孤立 cue 位移为 0、5 条 cue 一条不丢；
* 负时长被翻转为 `[2000,5000]` 且 `reasons` 含 `negative_duration`；
* 不可修复时间窗 → `infeasible_bounds` 且 `repaired_document is None`；
* 超预算夹具 → `budget_exceeded`，消息中给出数学最小位移；
* 坏时间戳/坏标记/坏编码 → `parse_failed` 与精确 failure_codes。

### 测试日志可关联输入与运行身份
* 每个会话写入 `tests/logs/pytest-<时间戳>-<随机>.log`，开头 `[ENV]` 记录
  subguard/Python/NumPy/FastAPI/pydantic/SQLite 版本；
* 每个用例有 `[CASE] BEGIN/END <nodeid>`；
* 管线/求解器日志以 `[run-id]` / `[job-id]` 开头（演示脚本用
  `demo-<fixture>`、HTTP 用 `run-xxxx`/`job-xxxx`），记录 DP 规模
  `n/candidates`、各阶段进度、最优总位移、判定依据（如
  “chain infeasible”“budget … refusing to force a squeeze”）。

---

## 6. 合成夹具（tests/fixtures/，全部本地生成）
| 文件 | 用途 |
|---|---|
| `chain_overlap.srt` | 连锁重叠 A>B>C（500/1000/200ms）+ 孤立合法 cue |
| `same_start.vtt` | 相同起点 + 负时长 + 零时长 |
| `multilingual.srt` | 中文/阿拉伯文/韩文/emoji/HTML 实体 + 跨 60s 边界 |
| `negative_duration.srt` | 负时长、500ms 过短、零时长 |
| `unfixable.srt` | 5s 段内两条 3s cue（配紧上限演示不可修复） |
| `budget_exceeded.srt` | 15 条级联重叠（可行但总位移超小预算） |
| `bad_timestamp.srt` `srt_position.srt` `bad_markup.vtt` `bad_encoding.srt` | 各类硬拒绝 |
| `clean.srt` | 完全合法 |

---

## 7. 复现步骤汇总

```bash
.venv/bin/python -m pytest tests/ -q                       # 全量（含穷举）
.venv/bin/python -m pytest tests/ -q -m "not exhaustive"   # 快速子集（<1s）
.venv/bin/python scripts/demo.py                          # 端到端演示
tail -f tests/logs/pytest-*.log                            # 查看关联日志
SUBGUARD_MAX_TOTAL_SHIFT_MS=2000 \
  .venv/bin/python scripts/demo.py --fixture \
  tests/fixtures/budget_exceeded.srt                       # 用环境变量复现预算拒绝
```

## 8. 设计边界（明确不做）
* 修复模型只做**时间平移 + 短时长延长 + 负时长翻转**；不会压缩、改写、删除正文，
  不会自动断句或重排文本。
* 仅支持第 3.1/3.2 节声明的受限子集；子集外特性一律硬诊断而非静默忽略。
* 作业为同步执行（本地合成数据规模下足够）；预留了 `queued/running` 状态，
  可在不改状态语义的前提下替换为后台 worker。
