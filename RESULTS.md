# 验证结果记录（RESULTS）

本文件记录在本机从**干净目录**按 `README.md` 步骤实际执行的结果。环境：

- 系统：Linux 6.8.0-90-generic (x86_64)，Ubuntu 24.04
- Python：3.12.3
- 日期（UTC）：2026-09-28
- 依赖：按 `requirements.txt` 固定版本，全新创建 `.venv` 后联网安装成功（无生产账号/外部业务数据）

## 复现步骤与结果

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt      # OK：fastapi 0.115.6 / uvicorn 0.34.0 /
                                               # pydantic 2.10.4 / python-multipart 0.0.20 /
                                               # cryptography 44.0.0 / pytest 8.3.4 / httpx 0.28.1
.venv/bin/python examples/make_samples.py      # OK：生成 13 个本地合成样本
.venv/bin/python -m pytest                     # OK：71 passed, 1 warning（仅第三方 anyio 弃用告警）
ARCHIVEGUARD_MAX_COMPRESSION_RATIO=50 \
  .venv/bin/uvicorn app.main:app --port 8137   # OK：/health 返回 version=1.0.0, python=3.12.3
```

## 独立测试覆盖（71 个，全部断言具体结果/失败类别）

- `test_canonical_resolver.py`：路径规范化、穿越/控制字符/反斜杠拒绝、POSIX 符号链接解析
  （含 `..` 穿过链接、链接环、自环、步数预算、解析后相对位置）。
- `test_policy_rejections.py`：相对穿越、绝对路径、同名覆盖、大小写碰撞、链接越界、链接环、
  硬链接、fifo、加密 ZIP、deflate64、tar.gz、垃圾魔数、截断 ZIP/TAR、条目数/深度/单文件预算、
  声明长度谎报、CRC 谎报、真实 deflate 炸弹；每个用例断言**具体 category** 且
  被拒绝运行**无 `output/`**，并用金丝雀文件断言隔离目录外无变化。
- `test_benign_extraction.py`：ZIP/TAR 正常字节落盘、隐式父目录、归档内符号链接可读、
  符号链接前缀在根内重定向、别名冲突拒绝、inspect 不落盘、重复运行状态隔离。
- `test_audit_and_logging.py`：HMAC 哈希链对接受/拒绝均校验通过、**直接改库可被检出**、
  每条日志关联 run id + 输入 SHA-256 + 版本、保留进度与判定依据、拒绝运行保留取证 input/log。
- `test_api.py`：真实 ASGI 栈；成功 200，拒绝 422/415 且带精确 category（绝不返回成功），
  runs/events/audit 端点、404 行为。

夹具独立性：`tests/fixtures/builder.py` 仅用标准库构造，ZIP 用 `struct` 字节级篡改
（声明长度、CRC、加密位、压缩方法），TAR 手写 ustar 头；预期类别在测试中硬编码，
**不调用被测核心生成参考答案**。

## 真实 HTTP 请求结果（curl，实测）

| 样本 | HTTP | verdict / category |
|---|---|---|
| good.zip / good.tar | 200 | extracted |
| traversal.zip | 422 | rejected / path_escape |
| case_collision.zip | 422 | rejected / case_collision |
| symlink_escape.zip | 422 | rejected / symlink_escape |
| symlink_loop.zip | 422 | rejected / symlink_loop |
| hardlink.tar | 422 | rejected / hardlink_rejected |
| fifo.tar | 422 | rejected / special_file_rejected |
| declared_lie.zip | 422 | rejected / declared_length_mismatch |
| crc_lie.zip | 422 | rejected / integrity_failure |
| bomb.zip（约 900x） | 422 | rejected / compression_bomb |
| tar_gz.tar.gz | 415 | rejected / unsupported_compression |
| encrypted.zip | 415 | rejected / unsupported_encryption |

隔离性实测：仅 2 个成功展开的运行目录含 `output/`；11 个被拒绝运行均**无** `output/`；
工作区根外未发现 `escape*`/`etc` 等逃逸产物。

审计实测：`GET /api/v1/audit/verify` → `{"ok": true, "checked": 60, ...}`；
直接用 SQL 改写某事件 `message` 后再次校验 →
`{"ok": false, "reason": "event_hash HMAC mismatch", "broken_at": 13}`（篡改检测 PASS）。

## 结论

在该干净环境下，文档步骤可完整复现，71 个独立测试全部通过，合成穿越/同名覆盖/链接链/
声明长度不符/压缩炸弹等夹具均被以具体类别拒绝，且拒绝时隔离目录外无任何变化；
审计哈希链与逐条判定日志可复核。未发现把异常或未知状态当作成功返回的路径。
