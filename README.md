# archguard — 本地归档安全检查与受控展开服务

一个**纯后端**服务，在本地对 ZIP / TAR（受限子集）归档做安全检查，只有当完整的
规范目标路径图被证明安全后才受控展开到隔离目录。全部数据、密钥、参与方均为
本地合成，不依赖任何生产账号或真实业务数据。

- 语言/运行时：Python 3.12
- Web：FastAPI + Uvicorn
- 状态：SQLite（标准库 `sqlite3`）
- 密码学：[`cryptography`](https://cryptography.io/)（HMAC-SHA256 签名清单）
- 测试：pytest + httpx（进程内 ASGI）

---

## 1. 它解决什么问题

展开一个不可信归档时，典型的攻击包括：相对/绝对路径穿越、同名覆盖、大小写
碰撞、硬链接、特殊文件、符号链接逃逸与链接环、声明长度不符、CRC 伪造、压缩
炸弹。本服务把"解析/取证"和"写盘"彻底分开：

1. **先解析全部成员的头部元数据**，不写任何一个字节；
2. **构建规范目标路径图**，在虚拟（纯词法）树中证明路径安全；
3. 预算（总量字节数、文件数、嵌套深度、压缩比、符号链接跳数）全部从
   **声明值**计算，炸弹在写盘前即被拒绝；
4. 图通过后，按 *目录 → 文件（物理路径）→ 符号链接* 的固定顺序展开；
5. 展开后用**独立的递归遍历**复核磁盘上每个节点的类型、大小、sha256 与
   计划一致；
6. 任一步失败即分类拒绝并回滚，隔离目录外不产生任何输出变化。

### 失败从不被伪装成成功

每个非成功结果都有稳定的失败类别（见
`app/archguard/errors.py:RejectionCategory`），未知异常归类为
`INTERNAL_ERROR` 且 run 状态为 `error`，绝不会返回成功。

| 类别 | 触发条件 |
|---|---|
| `PATH_TRAVERSAL` | 绝对路径、盘符、含 `..` 分量 |
| `PATH_INVALID` | NUL、反斜杠等结构非法名称 |
| `CASE_COLLISION` | 两个路径仅大小写不同 |
| `DUPLICATE_ENTRY` | 完全相同路径重复声明 |
| `PATH_CONFLICT` | 文件/目录占用同一前缀、经链接落到同一物理路径等 |
| `SYMLINK_ESCAPE` | 链接目标绝对路径/越出根/非法字节 |
| `SYMLINK_LOOP` | 链接成环或超过跳数预算 |
| `SYMLINK_DANGLING` | 链接指向归档未提供的目标 |
| `HARDLINK` / `ENTRY_SPECIAL` | 硬链接 / FIFO、设备、socket |
| `BUDGET_TOTAL_BYTES` / `BUDGET_FILE_COUNT` / `BUDGET_DEPTH` / `BUDGET_RATIO` | 预算超限（炸弹） |
| `DECLARED_SIZE_MISMATCH` | 实际产出字节数与头部声明不符 |
| `CONTENT_CRC_MISMATCH` | ZIP CRC32 校验失败 |
| `VERIFY_FAILED` | 展开后独立复核与计划不一致 |
| `CONTAINMENT_VIOLATION` | 解析路径越出隔离根 |
| `FORMAT_UNSUPPORTED` / `FORMAT_AMBIGUOUS` / `ARCHIVE_CORRUPT` | 格式问题 |
| `UPLOAD_LIMIT` / `INTERNAL_ERROR` | 上传过大 / 未预期内部错误 |

---

## 2. 工程结构（分层，可维护）

```
app/archguard/
  config.py            配置层：JSON 文件 + ARCHGUARD_* 环境变量 + 默认值
  errors.py            失败类别分类（判定依据的唯一来源）
  budget.py            预算与运行计数器
  archiveio/           规则/证据解析层：格式嗅探 + 统一 ZIP/TAR 读取抽象
  paths.py             安全内核：规范目标路径图、碰撞/逃逸/链接图解析
  kernel.py            受控展开 + 展开后独立复核
  isolation.py         状态隔离：每次运行独立目录、包含性谓词、留痕/回滚
  audit.py             审计接口：哈希链 JSONL + HMAC 签名清单
  store.py             SQLite 状态与审计查询
  engine.py            编排：scan→budget→graph→extract→verify→manifest
  api.py               FastAPI HTTP 接口（应用工厂）
  __main__.py          uvicorn 入口
scripts/inspect_archive.py     不起服务时的本地 CLI
tests/                 独立测试层（夹具用标准库独立构造）
  fixtures_archive.py  合成夹具构造器（不经过被测实现）
  conftest.py          服务夹具 + 隔离外"金丝雀"树 + 测试日志
  test_paths_unit.py   路径图单元测试（具体输入→具体类别）
  test_rejections.py   攻击夹具：精确类别 + 隔离外零变化断言
  test_accepted.py     合法归档：磁盘结果、物理路径、包含性
  test_budgets.py      压缩/数量/深度/比率炸弹
  test_audit_state.py  哈希链篡改检测、HMAC 签名、SQLite 关联
  test_api.py          HTTP 接口（进程内 ASGI）
```

测试的预期答案不是用被测核心自己生成的：夹具完全用 `zipfile`/`tarfile`/
`struct` 手工构造，期望值（sha256、字节数、磁盘布局、失败类别）在测试中
独立给出。

### 可复核的审计/日志

- `var/audit/events.jsonl`：追加式 JSONL，每条记录含 `run_id`（关联输入/
  运行身份）、`seq`、`version`、`phase/event`（进度与计算步骤）、`detail`
  （判定依据），并用 `prev_hash`/`record_hash`（SHA-256）串成哈希链。
- `var/manifests/<run_id>.json`：每次运行的签名清单（HMAC-SHA256，密钥为
  本地合成的 `var/audit/key.bin`），记录输入名、大小、sha256、判定与预算。
- 测试运行时所有审计记录另写入 `tests/logs/test-run.log`。

---

## 3. 快速开始（从干净目录复现）

需要 Python 3.12（在 3.12.3 上验证）。

```bash
# 1) 建虚拟环境并安装固定版本依赖
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 2) 跑全部测试
.venv/bin/python -m pytest tests/ -v

# 3) 起服务（默认 127.0.0.1:8080，数据根 ./var）
PYTHONPATH=app .venv/bin/python -m archguard --config config.example.json

# 也可用 Makefile：
make install   # 装依赖
make test      # 跑测试
make run       # 起服务
make demo      # 生成合成夹具并用 curl 逐个送检（见第 6 节）
```

服务只监听本机；所有输出都在 `--config` 里 `home` 指定的目录之下。

### 配置（`config.example.json`）

| 键 | 默认值 | 含义 |
|---|---|---|
| `home` | `./var` | 服务数据根（spool/runs/manifests/audit/runs.db 都在此） |
| `max_total_bytes` | 10485760 | 解压总量（声明值）上限 |
| `max_files` | 1000 | 常规文件数上限 |
| `max_depth` | 16 | 路径嵌套深度上限 |
| `max_symlink_hops` | 8 | 符号链接解析跳数上限 |
| `max_compression_ratio` | 100.0 | 声明压缩比上限 |
| `max_upload_bytes` | 52428800 | 单次上传字节上限 |
| `keep_failed_dirs` | false | 调试用：是否保留失败运行目录 |
| `host` / `port` | 127.0.0.1 / 8080 | 监听地址 |

任一项都可用环境变量覆盖，如 `ARCHGUARD_MAX_TOTAL_BYTES=1048576`。

---

## 4. HTTP 接口与请求样例

| 方法/路径 | 说明 |
|---|---|
| `POST /api/v1/inspect` | multipart 字段 `file` 上传一个归档，返回判定 |
| `GET  /healthz` | 版本、预算、计数 |
| `GET  /api/v1/runs?limit=` | 最近运行 |
| `GET  /api/v1/runs/{id}` | 单次运行的存储结果 |
| `GET  /api/v1/runs/{id}/events` | 该次运行的审计事件（进度/判定依据） |
| `GET  /api/v1/audit/verify` | 重算 JSONL 哈希链 |
| `GET  /api/v1/manifests/{id}` | 签名清单信封 |

合法归档：

```bash
curl -s -F "file=@benign.zip" http://127.0.0.1:8080/api/v1/inspect
# 200 -> {"accepted": true, "status": "accepted",
#         "files":[{"declared_path":"docs/readme.txt",
#                   "physical_path":"docs/readme.txt","size":13,"sha256":"..."}],
#         "verification":{"files":2,"symlinks":1,"directories":2}, ...}
```

恶意归档（422，带精确类别）：

```bash
curl -s -F "file=@traversal.zip" http://127.0.0.1:8080/api/v1/inspect
# 422 -> {"accepted": false, "status": "rejected",
#         "failure":{"category":"PATH_TRAVERSAL",
#                    "detail":"entry name contains a parent-directory component",
#                    "entry":"../../tmp/evil.sh"}, ...}
```

不起服务也可用 CLI：

```bash
PYTHONPATH=app .venv/bin/python scripts/inspect_archive.py path/to/archive.tar -v
```

---

## 5. 隔离与回滚保证

- 每个运行使用唯一 id，输出只落在 `var/runs/<id>/out/`，权限 `0700/0600`。
- 每个写盘目标都经过词法与 `realpath` 双重包含性检查（防既有符号链接重定向）。
- 展开顺序保证**没有任何一次文件写入会穿过新建符号链接**。
- 拒绝发生在写盘前（扫描/预算/路径图）则不创建输出目录；发生在写盘中
  （长度/CRC/复核）则删除整个 `var/runs/<id>/`；暂存的上传字节同步删除。
- 测试用隔离数据根**之外**的一棵"金丝雀"目录树，断言拒绝前后其哈希快照
  完全一致（见 `tests/conftest.py` 的 `canary`）。

---

## 6. 一键演示与实测结果

`make demo` 会：生成合成夹具（含穿越、大小写碰撞、链接环、硬链接、声明
长度不符、64MiB 压缩炸弹）→ 起服务 → 逐个 `curl` 送检 → 打印每个的 HTTP
状态与失败类别 → 校验哈希链 → 关服务。原始输出保存在 `demo-output.log`。

最近一次干净环境的实测记录见本仓库 **[RESULTS.md](RESULTS.md)**（依赖版本、
pytest 与 HTTP 实测均如实记录）。
