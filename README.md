# ArchiveGuard — 归档安全检查与受控展开服务（本地测试）

一个**纯后端**服务，用于在本地对归档做安全检查与受控展开，支持 **ZIP 与（未压缩的）TAR**
受限子集。所有数据均为本地合成夹具，无生产账号、无外部服务、无真实业务数据。

技术栈：**Python 3.12 · FastAPI · SQLite · cryptography**（标准库 `zipfile`/`tarfile` 负责格式解析）。

---

## 1. 它保证什么

1. **先构建规范目标路径图，再写任何字节。** 解析器只报告“头部声明了什么”（证据），
   安全内核据此在内存中计算每个条目的：
   - 规范路径（canonical）：词法规范化，拒绝 `..` 穿越、绝对路径、盘符/UNC、NUL/控制字符、反斜杠；
   - 物理落点（physical）：按 **POSIX 路径名解析语义**解析归档**自身**的符号链接（纯词法、
     不落盘、无 TOCTOU），拒绝符号链接越界、链接环、以及两个不同规范名指向同一物理路径的别名冲突；
   - 大小写碰撞（`A` 与 `a`）与完全同名覆盖。
2. **预算控制**：解压总字节数、单文件大小、条目数、目录嵌套深度、压缩比；符号链接解析步数有上限。
3. **硬链接与特殊文件（fifo/字符/块设备）明确拒绝**；加密 ZIP、deflate64 等受限子集外的压缩/加密拒绝；gzip/bzip2/xz 压缩的 tar 拒绝。
4. **所有输出限制在本次运行创建的隔离目录**内；文件用 `O_CREAT|O_EXCL|O_NOFOLLOW` 创建，
   流式 64 KiB 分块写入，运行期再次校验总预算、逐字节比对声明长度、依赖 zipfile 的 CRC-32 校验，
   并用**真实解压字节 / 真实存储字节**做精确压缩比判定。任何失败都回滚整个 `output/`。
5. **失败绝不会被当成成功**：每种拒绝都有**具体失败类别**（见下表），异常/未知状态返回显式错误。
6. **可复核**：每次运行有独立 run 目录与结构化 `run.log`（关联 run id、输入 SHA-256、版本、阶段进度、判定依据），
   审计写入 SQLite，事件之间用 **HMAC-SHA256 哈希链**（`cryptography` 提供）串联，可检测单行篡改。

### 失败类别（HTTP body 的 `detail.category`）

| 类别 | 含义 |
|---|---|
| `path_escape` | 路径穿越 / 绝对路径 / 盘符等逃逸 |
| `unsafe_name` | 空名、NUL/控制字符、反斜杠等 |
| `case_collision` / `duplicate_name` | 大小写碰撞 / 同名覆盖 |
| `type_conflict` / `symlink_alias` | 文件占用目录前缀 / 两名共指同一物理路径 |
| `symlink_escape` / `symlink_loop` / `symlink_rejected` | 链接越界 / 链接环 / 策略禁用 |
| `hardlink_rejected` / `special_file_rejected` | 硬链接 / 特殊文件 |
| `budget_total_size_exceeded` / `budget_file_size_exceeded` / `budget_entry_count_exceeded` / `budget_depth_exceeded` | 预算超限 |
| `compression_bomb` | 真实压缩比超限（流式展开后精确判定） |
| `declared_length_mismatch` | 头部声明长度 ≠ 实际解压字节数 |
| `integrity_failure` | CRC-32/校验和不符或 deflate 流损坏 |
| `unsupported_format` / `unsupported_compression` / `unsupported_encryption` | 格式/压缩/加密不在子集内 |
| `corrupt_archive` | 归档结构或 tar 流损坏（如截断） |
| `upload_too_large` | 上传体积超限 |

---

## 2. 工程结构（分层，非单文件、非调用壳、非固定返回）

```
app/
  config.py                 配置层：JSON + ARCHIVEGUARD_ 环境变量覆盖
  service.py                编排：接收 -> 解析 -> 规范图 -> (可选)受控展开，全程审计
  logging_setup.py          每次运行的结构化日志（run.log + 控制台 + 审计）
  kernel/                   安全内核
    model.py                  证据模型（EntryEvidence / EntryKind）
    parser.py                 格式探测与解析分发（拒绝压缩 tar）
    zip_parser.py             ZIP 证据解析（仅 stored/deflate、拒绝加密）
    tar_parser.py             纯 TAR 证据解析（mode "r:"，不自动解压）
    canonical.py              规范目标路径图：规范化 / 链接解析 / 冲突 / 预算
    extractor.py              受控流式展开、运行期校验、失败回滚
    errors.py                 失败类别分类体系
  isolation/workspace.py    状态隔离：每次运行一个全新 0700 目录
  audit/audit.py            SQLite + HMAC-SHA256 哈希链审计
  api/routes.py  schemas.py HTTP 接口（薄层）
  main.py                   应用装配 / 入口
config/default.json         默认配置（预算、策略、路径）
tests/                      独立测试层
  fixtures/builder.py         仅用标准库手工构造的合成夹具（含字节级篡改），预期结论硬编码
  conftest.py                 每测试独立 workspace/审计库、目录快照助手
  test_canonical_resolver.py  规范化/链接解析纯单元测试
  test_policy_rejections.py   各类拒绝 + “隔离目录外无变化”断言
  test_benign_extraction.py   正常展开字节、目录、链接正确性
  test_audit_and_logging.py   哈希链、防篡改、日志关联与判定依据
  test_api.py                 真实 ASGI 栈的 HTTP 行为
examples/make_samples.py    生成手工 curl 用的本地样本
```

设计要点：**参考答案不是由被测核心生成的**。`tests/fixtures/builder.py` 用标准库独立构造归档，
对 ZIP 用 `struct` 直接篡改本地/中央目录头（声明长度、CRC、加密位、压缩方法），对 TAR 手写 512 字节
ustar 头；每个夹具的预期类别在测试中硬编码断言，而不是调用内核得到。

---

## 3. 从干净目录复现

需要 Python ≥ 3.12（开发与验证使用 3.12.3，Linux）。

```bash
# 1) 进入项目目录后创建虚拟环境并安装固定版本依赖
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 2) 生成手工请求用的合成样本（写入 examples/samples/，已被 gitignore）
.venv/bin/python examples/make_samples.py

# 3) 运行全部独立测试
.venv/bin/python -m pytest

# 4) 启动本地服务（相对路径相对当前工作目录解析）
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8137
```

### 依赖版本（`requirements.txt`，实测安装）

```
fastapi==0.115.6
uvicorn[standard]==0.34.0
pydantic==2.10.4
python-multipart==0.0.20
cryptography==44.0.0
pytest==8.3.4
httpx==0.28.1
```
（Python 3.12.3；传递依赖见已验证的安装输出。）

---

## 4. 请求样例

健康检查：
```bash
curl -s http://127.0.0.1:8137/health
```

只检查、不展开（构建规范图并返回动作计划）：
```bash
curl -s -X POST http://127.0.0.1:8137/api/v1/archives/inspect \
  -F file=@examples/samples/good.zip
```

受控展开：
```bash
curl -s -X POST http://127.0.0.1:8137/api/v1/archives/extract \
  -F file=@examples/samples/good.tar
```

攻击样本（注意 HTTP 状态码与 `detail.category`）：
```bash
for f in traversal.zip case_collision.zip symlink_escape.zip symlink_loop.zip \
         hardlink.tar fifo.tar declared_lie.zip crc_lie.zip bomb.zip \
         tar_gz.tar.gz encrypted.zip; do
  code=$(curl -s -o /tmp/r.json -w "%{http_code}" \
    -X POST http://127.0.0.1:8137/api/v1/archives/extract -F file=@examples/samples/$f)
  echo "$f -> HTTP $code $(python3 -c "import json;d=json.load(open('/tmp/r.json'))['detail'];print(d['category'])")"
done
```

审计与运行查询：
```bash
curl -s "http://127.0.0.1:8137/api/v1/audit/verify"          # 校验 HMAC 哈希链
curl -s "http://127.0.0.1:8137/api/v1/runs?limit=50"          # 列出运行
curl -s "http://127.0.0.1:8137/api/v1/runs/<run_id>"          # 单次运行结论
curl -s "http://127.0.0.1:8137/api/v1/runs/<run_id>/events"   # 逐步事件与判定依据
```

---

## 5. 配置

`config/default.json`（可被环境变量覆盖）：

| 键 | 默认值 | 环境变量覆盖 |
|---|---|---|
| `max_upload_bytes` | 50 MiB | `ARCHIVEGUARD_MAX_UPLOAD_BYTES` |
| `budgets.max_total_uncompressed_bytes` | 64 MiB | `ARCHIVEGUARD_MAX_TOTAL_UNCOMPRESSED_BYTES` |
| `budgets.max_file_size_bytes` | 16 MiB | `ARCHIVEGUARD_MAX_FILE_SIZE_BYTES` |
| `budgets.max_entries` | 500 | `ARCHIVEGUARD_MAX_ENTRIES` |
| `budgets.max_depth` | 20 | `ARCHIVEGUARD_MAX_DEPTH` |
| `budgets.max_compression_ratio` | 100 | `ARCHIVEGUARD_MAX_COMPRESSION_RATIO` |
| `budgets.symlink_resolution_steps` | 40 | `ARCHIVEGUARD_SYMLINK_RESOLUTION_STEPS` |
| `policy.allow_case_collisions` | false | `ARCHIVEGUARD_ALLOW_CASE_COLLISIONS` |
| `policy.allow_symlinks` | true | `ARCHIVEGUARD_ALLOW_SYMLINKS` |
| `workspace_root` / `audit_db` | `./.runs` / `./.audit/audit.db` | `ARCHIVEGUARD_WORKSPACE_ROOT` / `ARCHIVEGUARD_AUDIT_DB` |

示例：以更严格的压缩比运行：
```bash
ARCHIVEGUARD_MAX_COMPRESSION_RATIO=50 .venv/bin/uvicorn app.main:app --port 8137
```

---

## 6. 输出与状态布局

```
.runs/runs/<run_id>/
  input/<name>      受检字节（拒绝时保留取证，仅在本次 run 目录内）
  output/           仅在通过预检且成功展开后存在；任何失败即整体删除
  run.log           结构化 JSONL：run id / 输入 SHA-256 / 版本 / 阶段 / 结论 / 类别 / 证据
.audit/audit.db     SQLite 审计库
.audit/audit_key.bin 本地 HMAC 密钥（0600，首用生成，无外部 KMS）
```

---

## 7. 安全边界与说明（如实记录）

- 支持**未压缩 tar**（ustar/pax/gnu 长名由 tarfile 处理）；gzip/bzip2/xz 外层压缩按受限子集显式拒绝。
- 符号链接允许指向归档内、允许悬空；但绝对目标、越界目标、链接环一律拒绝。普通文件**写入其解析后的物理路径**，
  因此归档内链接无法把写操作重定向到根外。
- “压缩炸弹”采用两层防御：头部总/单文件大小预算先挡住“谎报巨大尺寸”，精确压缩比在**实际解压后**判定，
  避免把“声明长度不符”误报成炸弹（测试明确断言二者类别不同）。
- 本服务面向本地受控测试，不内置鉴权/多租户隔离；请勿暴露到不可信网络。
