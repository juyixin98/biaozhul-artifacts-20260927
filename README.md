# subtitle-validator

纯后端字幕校验与修复建议服务，支持 **SRT** 与 **WebVTT 受限子集**。
输入一份字幕，服务返回分类诊断（负时长 / 重叠 / 过短显示 / 跨片边界等）以及一个
**最小时间位移**的修复建议；修复受最大改动预算约束，无解或超预算时如实报告，
绝不强行挤压、绝不删除字幕。

技术栈：Python 3.12 · FastAPI · NumPy · SQLite（标准库 `sqlite3`）· pytest。
所有数据均为本地合成夹具（`fixtures/`），无外部账号与真实业务数据。

## 目录结构

```
app/
  main.py            # 服务入口（uvicorn app.main:app）
  config.py          # 配置层（SUBVAL_* 环境变量）
  errors.py          # 结构化解析错误
  service.py         # 编排：解析 -> 诊断 -> 求解 -> 生成修复原因
  parsing/           # 媒体解析层
    timeparse.py     #   严格时间戳解析/格式化（SRT HH:MM:SS,mmm / VTT [HH:]MM:SS.mmm）
    encoding.py      #   UTF-8(BOM) 解码、换行归一化；非法编码显式报错
    srt.py / vtt.py  #   格式解析（VTT 子集：WEBVTT 头、NOTE、cue；STYLE/REGION 显式拒绝）
    render.py        #   修复后渲染：只改时间戳，文本/标记/标识符逐字节保留
  kernel/            # 时间与信号内核
    diagnostics.py   #   诊断：NEGATIVE_DURATION/ZERO_DURATION/TOO_SHORT/OVERLAP/
                     #   OUT_OF_ORDER/MEDIA_BOUNDARY_EXCEEDED
    solver.py        #   整数网格上的精确最小位移 DP（NumPy 向量化）
  jobs/store.py      # 作业状态：SQLite jobs 表 + 状态迁移事件表
  api/               # 验证接口层（路由 + pydantic 请求模型）
tests/               # 独立测试（含穷举小网格核验）+ 结构化运行日志
fixtures/            # 连锁重叠 / 相同起点 / 多语字符 / 不可修复时间窗 / 干净样本
scripts/demo.py      # 本地演示脚本
```

## 安装与运行

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 启动服务（数据库默认 ./data/subval.db，可用 SUBVAL_DB_PATH 覆盖）
.venv/bin/uvicorn app.main:app --port 8000

# 本地演示（进程内跑通全部夹具并校验预期结果，失败则非零退出）
.venv/bin/python scripts/demo.py

# 测试（实际执行并输出结构化运行日志 tests/logs/run-*.jsonl）
.venv/bin/python -m pytest -v
```

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/healthz` | 存活探针 |
| GET | `/v1/meta` | 版本信息（app/python/numpy/fastapi/pydantic） |
| POST | `/v1/validations` | 提交字幕校验，同步返回完整结果与 `job_id` |
| GET | `/v1/validations/{job_id}` | 查询作业状态、结果与状态迁移事件 |

POST 请求体：

```json
{
  "format": "srt",
  "content": "1\n00:00:01,000 --> 00:00:04,000\nhi\n",
  "media_duration_ms": 300000,
  "options": {
    "min_duration_ms": 1000,
    "min_gap_ms": 0,
    "budget_ms": 5000,
    "resolution_ms": null,
    "allow_approximate": false,
    "max_grid": null
  }
}
```

## 错误语义

### HTTP 层

| 状态 | 含义 |
|---|---|
| 201 | 校验完成。**不代表没有发现问题**——结果看 `repair.status` |
| 404 | `JOB_NOT_FOUND`：未知的 job_id |
| 422 | 输入无法解析（`PARSE` 类错误码）或求解配置被拒绝（`INVALID_*`）；作业记为 `FAILED`，错误体带 `job_id` 可追溯 |

### 解析错误码（422，作业 FAILED）

`INVALID_ENCODING`（非 UTF-8）、`BAD_HEADER`（缺 WEBVTT 头）、`BAD_COUNTER`（SRT 序号非整数）、
`BAD_TIMING`（缺少/畸形 `-->` 行）、`BAD_TIMESTAMP`（时间戳不合严格格式，含行号与原文）、
`EMPTY_CUE`、`UNSUPPORTED_BLOCK`（STYLE/REGION 超出子集）。

### 诊断码（结果内 `diagnostics[]`）

| 码 | 严重级 | 含义 |
|---|---|---|
| `NEGATIVE_DURATION` / `ZERO_DURATION` | error | 结束早于/等于开始 |
| `TOO_SHORT` | warning | 显示时长小于 `min_duration_ms` |
| `OVERLAP` | error | 相邻 cue 时间重叠（`overlap_ms`、`same_start` 标记相同起点） |
| `OUT_OF_ORDER` | warning | 文件顺序与时间顺序不一致 |
| `MEDIA_BOUNDARY_EXCEEDED` | error | 越过 `[0, media_duration_ms]` 片边界 |

### 修复状态（`repair.status`）

| 状态 | 含义 |
|---|---|
| `ALREADY_VALID` | 无诊断，未改动 |
| `SOLVED` | 给出最小位移方案 `proposal` 与 `repaired_content` |
| `BUDGET_EXCEEDED` | 最小所需位移（`minimal_change_ms`）超过预算，**不输出方案** |
| `UNSOLVABLE` | 在时间窗内不存在可行排布（如 4×1000ms 塞进 3000ms），不输出方案 |
| `TOO_LARGE` | 网格规模超过 `max_grid`，拒绝静默近似；可提高 `max_grid` 或指定 `resolution_ms` |

未知/异常状态不会统一返回成功：解析失败是 422 + FAILED 作业；不可解与超预算是显式状态且
`proposal=null`；每条 cue 的修复都附 `reasons` 与原始时间、原文。

## 求解器说明

- 目标函数：`Σ(|start−start0| + |end−end0|)`（总 L1 位移，毫秒）。
- 约束：每条时长 ≥ `min_duration_ms`；相邻（按文件顺序）间隔 ≥ `min_gap_ms`；
  所有时间落在 `[0, horizon]`，`horizon = media_duration_ms`（若给定），否则
  `max(原始时间点) + packed`（可证最优解不会超出该界：否则整体前移可严格降代价）。
- 算法：网格上的精确动态规划（NumPy 向量化，分块控制内存）。默认分辨率为输入时间的
  gcd，结果精确；自定义 `resolution_ms` 必须整除 gcd，否则报 `INVALID_RESOLUTION`，
  除非显式 `allow_approximate=true`（此时 trace 标记 `approximate`）。
- 复杂度 `O(n · T²)`（T 为网格点数），`max_grid` 限制规模，超出报 `TOO_LARGE`。
- 保证：不删 cue（`proposal` 条数恒等于输入 cue 数）、不输出违反约束的方案、
  超预算不硬挤。

## 测试与可复核性

- `tests/test_solver.py`：手算期望值（如同起点对 → 2500ms 唯一最优、负时长 → 2000ms），
  非由被测实现生成。
- `tests/test_solver_grid.py`：**穷举小规模时间网格**（n=1 全 0..6、n=2 全 0..4 多组
  media、n=3 固定种子抽样），与独立的暴力枚举器（递归枚举全部合法排布，与 DP 无共享代码）
  逐一比对目标值与约束；无媒体边界时暴力枚举视界故意大于求解器上界，以核验上界正确性。
- `tests/test_api.py`：断言具体结果与失败类别（UNSOLVABLE 不得伪装成功、解析失败 → 422 +
  FAILED 作业、未知作业 → 404、事件链 PENDING→RUNNING→DONE）。
- 运行日志：每次测试写 `tests/logs/run-<时间>-<run_id>.jsonl`，包含运行身份 run_id、
  版本（python/numpy/fastapi/pydantic/pytest）、逐条用例结果与进度 `k/total`，以及
  关键用例的输入哈希、期望/实际值与判定依据（`rlog` 记录）。

## 复现步骤

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest -v        # 57 passed
.venv/bin/python scripts/demo.py     # DEMO OK
.venv/bin/uvicorn app.main:app       # 起服务后 POST /v1/validations
```

## 限制

- WebVTT 仅支持子集：头部、NOTE、cue（标识符/设置保留）；STYLE/REGION 显式拒绝。
- 内部换行归一化为 `\n`；文本内容本身逐字节保留。
- 精确求解面向中小规模输入；超长媒体需调 `resolution_ms`/`max_grid`（见求解器说明）。
