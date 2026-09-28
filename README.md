# 本地 MPEG-TS 分析后端

一个**完全本地**的 MPEG Transport Stream（ISO/IEC 13818-1 / ITU-T H.222.0）分析服务。
支持 188 字节 TS 包、有限扫描同步恢复、PAT/PMT 表解析与原子版本切换、
逐 PID 连续计数器检查、适配字段（含 PCR、discontinuity 标记）解析、
以及**受限**的 PES 重组。所有输入均为本地合成夹具，不依赖任何生产账号或真实业务数据。

技术栈：Python 3.12 · FastAPI · NumPy · SQLite（标准库）· pytest。

## 工程结构（各模块有真实职责）

```
app/
  config.py            运行配置（均可由 TSANALYZER_* 环境变量覆盖）
  diagnostics.py       结构化诊断：记录标识、严重级别、脱敏上下文
  demux.py             顶层流水线：同步 → 包头 → CC → 表/PES/PCR 调度
  core/
    packets.py         188 字节包头与适配字段纯解析（无状态）
    sync.py            NumPy 向量化同步字节扫描 + 有限范围确认/恢复
    continuity.py      逐 PID 连续计数器状态机
    psi.py             PSI 节重组（pointer_field）与 PAT/PMT 节解析
    tables.py          节目映射存储：CRC 校验、版本切换原子发布
    pes.py             受限 PES 重组（长度、PTS/DTS、缺口、容量上限）
    crc32.py           MPEG-2 CRC-32（表驱动）
    timing.py          PCR 时序/信号内核（间隔、抖动、传输速率）
  jobs/
    store.py           SQLite：作业、全部诊断事件、报告
    manager.py         线程池作业生命周期（queued/running/done/failed）
  api/
    schemas.py         Pydantic 响应模型
    routes.py          /jobs* 异步作业接口 + /validate 同步三态判定
    main.py            FastAPI 应用工厂
tests/
  fixtures/
    ts_builder.py      独立夹具构造器（独立 CRC 实现，不依赖被测代码）
    data/              可复用的 *.ts 夹具与 *.expect.json 旁车文件
  test_*.py            按内核划分的断言式测试
scripts/
  validate.py          不启动服务即可跑完全部场景断言
  generate_fixtures.py 把场景固化成 *.ts 文件供外部工具复放
docs/
  boundary-semantics.md  边界语义说明
  checks-not-run.md      明确列出“未执行/无法执行”的检查
```

## 快速开始

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt          # 或 pip install -r requirements-lock.txt

# 1) 纯内核验证（不需要 HTTP）
python scripts/validate.py

# 2) 测试套件
python -m pytest

# 3) 生成可复用 .ts 夹具到 tests/fixtures/data/
python scripts/generate_fixtures.py

# 4) 启动 HTTP 服务
uvicorn app.main:app --reload
```

## HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/jobs` | 上传 `multipart/form-data`（字段名 `file`）原始 `.ts` 字节，返回 `job_id`（202） |
| `GET`  | `/jobs` | 列出作业（含状态、输入大小、sha256） |
| `GET`  | `/jobs/{id}` | 单个作业状态 |
| `GET`  | `/jobs/{id}/report` | 完整分析报告（节目映射、CC、PES、PCR、诊断） |
| `GET`  | `/jobs/{id}/events?limit=&offset=` | 诊断事件分页（全部事件持久化在 SQLite） |
| `POST` | `/validate` | 同步一次性判定，返回 `accepted` / `rejected` / `indeterminate`；可用 `X-Request-Id` 指定记录标识 |
| `GET`  | `/health` | 健康检查 |

快速试用：

```bash
curl -F "file=@tests/fixtures/data/clean.ts;type=video/mp2t" \
     -H "X-Request-Id: demo-1" http://127.0.0.1:8000/validate
```

`/validate` 的三态语义：

* **accepted**：至少解析到一个包，且无 error 级诊断；warning（如重复包）仍可接受。
* **rejected**：存在 error 级诊断（CRC 错误、真实缺包、CC 非法复用、坏起始码等）。
* **indeterminate**：同步在有限扫描窗内无法锁定，或根本没有完整 188 字节包——**不猜测**。

## 诊断设计

每条诊断都带 `record_id`（作业 id 或 `X-Request-Id`）、顺序号 `seq`、
稳定的事件代码（如 `continuity_lost`、`table_crc_error`、`discontinuity_indicator`）、
严重级别、PID、字节偏移与**解释接受/拒绝/无法判定原因**的上下文。
上下文中的字节串一律脱敏为 `<bytes:N>`，长字符串截断，不打印任何原始净荷。

## 关键算法契约（摘要）

* **同步**：NumPy 向量化找候选 `0x47`，再按 188 周期确认多个同步字；
  恢复扫描受 `max_sync_scan_bytes` 限制，超时即判定 `indeterminate`，绝不无限扫描。
* **连续计数器（逐 PID 独立）**：只有**带负载**的包才递增 CC；
  adaptation-only 包应重复 CC（递增者按常见实现容忍并记录 info）；
  CC 重复且负载相同 = 重复包（重组时不二次追加）；
  CC 重复但负载不同 = 非法复用（error）；CC 跳变 = 真实缺包。
* **discontinuity 标记 ≠ 真实缺包**：适配字段 `discontinuity_indicator=1`
  是发送方的显式声明，接受当前 CC 且**不计**丢包，单独记录事件。
* **表版本原子更新**：只有完整且 CRC 正确、`current_next=1` 的节才发布；
  同一版本重传记为 repeat；版本切换是一次原子替换（同步裁剪不再被引用的旧 PMT）。
* **PES**：仅重组 PMT 声明的 ES PID；起始码/PTS/DTS/声明长度被解析但不解码媒体；
  单 PID 同时只缓冲一个 PES，受 `max_pes_payload_bytes` 限制；
  CC 缺包会把打开的 PES 标记为 `gap` 而不是把缺口两侧字节静默拼接。

完整边界（length=0、填充、TEI、加扰、未知 PID、指针字段等）见
[`docs/boundary-semantics.md`](docs/boundary-semantics.md)。
明确**未执行**的检查（媒体解码、实时流、PCR 精度门限等）见
[`docs/checks-not-run.md`](docs/checks-not-run.md)。
