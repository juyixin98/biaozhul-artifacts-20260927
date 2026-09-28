# 实测结果（RESULTS.md）

本文件如实记录在干净目录中复现本服务的步骤与结果。所有数据均为本地合成，
无任何生产账号或真实业务数据。

- 日期：2026-09-28
- 主机：Linux 6.8.0-90-generic x86_64（Ubuntu）
- Python：3.12.3
- 复现入口：`make install && make test && make demo`（或下文逐条命令）

## 1. 依赖版本

固定于 `requirements.txt`，关键组件：

```
fastapi==0.141.1
uvicorn==0.54.0
starlette==1.7.0
pydantic==2.13.5
cryptography==50.0.1
python-multipart==0.0.32
httpx==0.28.1
pytest==9.1.1
anyio==4.15.1
```

## 2. 单元/集成测试

命令：

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest tests/ -v
```

结果：**78 passed**（约 1.5s，0 failed / 0 error / 0 skip）。

覆盖（断言的是具体结果与失败类别，而非"接口可调用"）：

- `test_paths_unit.py`：名称/链接目标规范化的逐输入断言；重复、大小写碰撞、
  文件/目录前缀冲突、隐式目录、深度预算、自环、悬空链接。
- `test_rejections.py`：5 种 ZIP 穿越变体、TAR 穿越/绝对路径、精确重复、
  大小写碰撞、链接逃逸/环/悬空/目录链接逃逸、硬链接、FIFO/字符设备、
  ZIP FIFO 模式位、ZIP 声明长度不符、ZIP CRC 不符、TAR 声明长度不符、
  非归档格式。每个用例都断言：
  1. `status=rejected` 且 `failure.category` 等于**精确**类别；
  2. `var/runs/` 无残留目录、`var/spool/` 无残留字节；
  3. 隔离数据根**之外**的金丝雀目录树拒绝前后 sha256 快照一致；
  4. SQLite/审计中同一 `run_id` 关联到相同类别。
- `test_accepted.py`：ZIP/TAR 合法展开的磁盘内容、字节数、独立 sha256、
  相对/目录符号链接的物理落点、`.`/重复分隔符归一化、全部输出包含在 home。
- `test_budgets.py`：总量炸弹、文件数炸弹、深度炸弹、压缩比炸弹（在写盘前
  仅凭声明值拒绝），以及限额内正常接受。
- `test_audit_state.py`：哈希链正向校验、**篡改第 3 行后在第 3 行断链**、
  HMAC 清单签名校验、**篡改清单后签名失效**、HMAC 往返、拒绝运行同样有签名
  清单、SQLite 终态计数。
- `test_api.py`：8 个 HTTP 用例（健康检查、200/422、非归档 422、运行列表/
  事件、未知 id 404、哈希链接口、签名清单接口），进程内 ASGI，无真实端口依赖。

测试审计日志写入 `tests/logs/test-run.log`，每条含时间、级别、运行 id（方括号
中的 8 位前缀）、阶段/事件与判定细节，例如：

```
... INFO archguard.audit [26551a26] receive/run_start {"service_version":"1.0.0",
    "input_sha256":"ffc911b4...","input_size":438,"budgets":{...}}
... INFO archguard.audit [26551a26] scan/budgets_checked {"declared_total":14,...}
```

夹具由 `tests/fixtures_archive.py` 仅用标准库（`zipfile`/`tarfile`/`struct`）
独立手工构造，预期答案在各测试中独立给出，不来自被测核心。

## 3. HTTP 端到端实测

命令：`bash scripts/demo.sh`（生成夹具 → 起 uvicorn → 逐个 curl 送检 →
校验哈希链 → 关服务；原始输出见 `demo-output.log`）。

| 合成夹具 | HTTP | 失败类别 | 关联条目 |
|---|---:|---|---|
| benign.zip（2 文件 + 1 相对符号链接） | 200 | ACCEPTED | — |
| traversal.zip（`../../tmp/evil.sh`） | 422 | PATH_TRAVERSAL | `../../tmp/evil.sh` |
| absolute.zip（`/tmp/abs-evil`） | 422 | PATH_TRAVERSAL | `/tmp/abs-evil` |
| case-collision.zip（Data.TXT/data.txt） | 422 | CASE_COLLISION | `data.txt` |
| duplicate.zip（同名两次） | 422 | DUPLICATE_ENTRY | `a.txt` |
| link-loop.zip（a→b→a） | 422 | SYMLINK_LOOP | `a` |
| link-escape.zip（→`../../../etc/passwd`） | 422 | SYMLINK_ESCAPE | `l` |
| dangling.zip（指向缺失文件） | 422 | SYMLINK_DANGLING | `l` |
| hardlink.tar（TAR 硬链接） | 422 | HARDLINK | `hl` |
| fifo.tar（特殊文件） | 422 | ENTRY_SPECIAL | `spooky` |
| declared-size.zip（头声明 99，实产 5 字节） | 422 | DECLARED_SIZE_MISMATCH | `a.txt` |
| bad-crc.zip（翻转载荷字节） | 422 | CONTENT_CRC_MISMATCH | `a.txt` |
| zip-bomb.zip（压缩后约 64 KiB，声明 64 MiB 零） | 422 | BUDGET_TOTAL_BYTES | — |
| not-archive.dat（纯文本） | 422 | FORMAT_UNSUPPORTED | — |

接受样例（benign）响应摘要：

```json
{"accepted": true, "status": "accepted",
 "files": [
   {"declared_path":"docs/readme.txt","physical_path":"docs/readme.txt",
    "size":13,"sha256":"..."},
   {"declared_path":"docs/sub/note.txt","physical_path":"docs/sub/note.txt",
    "size":11,"sha256":"..."}],
 "verification": {"files":2,"symlinks":1,"directories":2}}
```

## 4. 隔离 / 回滚 / 审计实测

- `GET /api/v1/audit/verify` → `{"ok":true,"records":63}`：全部 14 次送检的
  JSONL 哈希链完整（每次运行约 4–10 条事件，拒绝点不同事件数不同）。
- 14 次送检后 `var/runs/` **仅保留 1 个目录**（唯一被接受的 benign），其余 13
  个拒绝全部回滚；`var/spool/` 为空。
- 接受运行目录中符号链接解析仍在根内：
  `link -> docs/readme.txt`，无越界写入。
- 任一拒绝运行的事件均与 `run_id` 关联并记录判定依据，例如：
  `complete/rejected {"category":"PATH_TRAVERSAL",
  "detail":"entry name contains a parent-directory component",
  "entry":"../../tmp/evil.sh"}`，且记录携带 `prev_hash`/`record_hash`。
- 接受与拒绝运行均产生 HMAC-SHA256 签名清单 `var/manifests/<run_id>.json`；
  离线用 `var/audit/key.bin` 校验通过，篡改清单内容后校验失败（由
  `test_manifest_tampering_invalidates_signature` 自动断言）。

## 5. 复现命令汇总

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest tests/ -v          # 78 passed
bash scripts/demo.sh                          # 端到端 HTTP 实测
# 或长期运行：
PYTHONPATH=app .venv/bin/python -m archguard --config config.example.json
```

## 6. 已知边界（如实说明）

- 仅支持 ZIP 与**未压缩** POSIX TAR 子集；gzip/bzip2/xz TAR、RAR、7z 等返回
  `FORMAT_UNSUPPORTED`。
- 展开本身不保留归档内的可执行位等模式信息（文件 `0600`、目录 `0700`），
  这是受控展开的刻意策略，而非缺陷。
- 服务设计为单机本地使用（默认监听 127.0.0.1，HMAC 密钥为本地随机合成）；
  未包含多用户鉴权与横向扩展能力，不属于本次本地测试目标。
