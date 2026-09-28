# 离线对象存储策略差分分析（Offline Policy Diff Analyzer）

对**旧/新版对象存储策略**在显式受限请求空间（资源前缀 × 操作集合 × 主体 ×
条件区间）内做**穷举差分**，回答唯一关键问题：

> 新策略是否扩大了可访问请求集合？扩大在哪些**具体请求**上？哪些点因条件未知
> 而**无法确定**（且绝不默认允许）？

全部数据为本地合成夹具：无生产账号、无真实业务数据、无外网调用。
信任锚点是本地生成的单个 Ed25519 公钥；**不构建用户/角色后台**。

算法细节见 [`docs/ALGORITHM.md`](docs/ALGORITHM.md)。

## 安全语义（四条硬不变量）

1. **默认拒绝**：无确定 ALLOW 即 DENY（响应中 `default_deny: true`）。
2. **显式拒绝优先**：确定命中的 DENY 压过 ALLOW，与规则顺序无关。
3. **未知不放行**：条件含未知/不可比值时为 `UNKNOWN`；否定条件
   （`NotEq/NotIn/NotCidrMatch/...`）在属性缺失时同样是 UNKNOWN。
4. **禁止策略显式写出 UNKNOWN 效果**；UNKNOWN 只能由三态内核算出。

## 本地验证（一条命令）

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[test]'
bash scripts/verify.sh
```

`verify.sh` 依次执行并在任一步失败时非零退出：

| 步骤 | 内容 | 预期判断 |
|---|---|---|
| 1 | 生成/复用 `fixtures/keys/` 下 Ed25519 合成密钥对 | 写出 `submitter_public/private.pem` |
| 2 | `pytest -v` 全套 **55 个独立测试** | `55 passed` |
| 3 | `scripts/demo.py` 端到端穷举 + 证据对账 | `verdict=WIDENED_WITH_UNKNOWN`，见证资源恰为 `logs-secret/0`，证据 `contradictions=0` |
| 4 | 启动真实 uvicorn + HTTP 签名/差分/坏签名冒烟 | 返回新增允许见证；坏签名 `422 CRYPTO_BAD_SIGNATURE`；`ALL VERIFICATION STEPS PASSED` |

### 单独运行

```bash
.venv/bin/python -m pytest tests/ -v          # 独立测试（含独立参考判定）
.venv/bin/python scripts/demo.py              # 不启服务的端到端演示
.venv/bin/python -m uvicorn diffanalyzer.main:app --port 8080
DIFF_DB_PATH="data/x.db" ...                  # 覆盖 SQLite 路径
```

## HTTP 接口（审计接口，非账号后台）

| 方法/路径 | 作用 |
|---|---|
| `GET /health` | 语义开关：默认拒绝、显式拒绝优先 |
| `GET /v1/trust` | 受信任提交者公钥清单（`user_backend: none`） |
| `POST /v1/policies` | 提交**签名策略信封**（验签→严格解析→不可变落库） |
| `GET  /v1/policies` | 列出策略版本快照 |
| `POST /v1/evidence` | 提交签名证据束（哈希链重算 + 验签） |
| `POST /v1/diffs` | 发起穷举差分，返回总结论/转移计数/空间描述/**具体见证**/证据对账 |
| `GET  /v1/diffs/{id}` | 取回持久化差分 |
| `GET  /v1/audit?request_id=..&actor=..&diff_id=..` | 审计；**failures 与 inconclusive 单列** |

请求可用 `X-Request-Id` 固定身份、`X-Actor` 标注操作者；二者贯穿全部日志。

### 失败类别（结构化，非自由文本）

`SCHEMA_INVALID`(400) · `SCHEMA_VERSION_CONFLICT`(409) · `SCOPE_NO_OVERLAP`(400) ·
`SCOPE_UNIVERSE_DEFINITION`(400) · `SCOPE_SPACE_TOO_LARGE`(400) ·
`CRYPTO_MISSING_SIGNATURE`/`CRYPTO_BAD_SIGNATURE`/`CRYPTO_UNREGISTERED_KEY`(422) ·
`EVIDENCE_TAMPERED`(422) · `NOT_FOUND`(404)

## 目录结构

```
diffanalyzer/
  parser.py         # 规则/证据 JSON 严格解析、前缀规范化、未知字段拒绝
  kernel.py         # 三态安全内核（默认拒绝/显式拒绝优先），无 I/O
  universe.py       # 前缀区域完备分割 + 有限维构造 + 确定性穷举
  evidence.py       # 历史证据解析与哈希链重算（区分篡改/伪造）
  crypto_verify.py  # Ed25519 验签、规范 JSON、静态公钥注册表
  diffengine.py     # 穷举分桶、具体见证、证据对账
  store.py          # SQLite：策略不可变快照、结果持久化、只追加审计
  audit.py          # 请求身份贯穿；失败与不确定单列
  service.py        # 用例编排（解析→验签→状态→内核）
  api.py / main.py  # FastAPI 审计接口 / ASGI 入口
  config.py         # config.toml 加载（tomllib，标准库）
  local_signing.py  # 仅夹具/演示使用的签名构造助手（内核不依赖）
fixtures/           # 本地合成密钥（gen_keys.py）
scripts/            # demo.py / http_smoke.py / verify.sh
tests/              # 55 个独立测试（见下“独立性”）
docs/ALGORITHM.md   # 算法假设与完备性论证
```

## 测试如何防止“参考答案全由被测核心自身生成”

- `tests/test_diffengine.py::test_exhaustive_transition_table_against_independent_oracle`
  内含一份**独立重写**的 `reference_decide`（不 import kernel/diffengine），
  在受限空间每个点上分别用参考实现与内核计算 `(old,new)`，要求完全一致，
  并用参考实现独立统计新增允许数与引擎计数对账。
- 所有见证都用**测试手写的具体请求**（如 `acct/bob` GET `logs-secret/0`）
  重新调用两版内核复演，断言 `(DENY,ALLOW)` 等具体转移。
- 测试断言具体字段与失败类别（如 `CRYPTO_MISSING_SIGNATURE`、
  `EVIDENCE_TAMPERED` + 失配 `seq`、区域 anchor），不只检查“接口能调用”。
- 覆盖：重叠前缀边界、否定条件+未知值、范围外无关规则变化（EQUIVALENT 零见证）、
  收窄、证据篡改/换序/伪造/矛盾/INCONCLUSIVE、空间上限拒绝抽样、版本不可覆盖。

## 依赖版本（Python 3.12，完整锁定见 requirements-lock.txt）

- fastapi 0.141.1（starlette 1.7.0，pydantic 2.13.5）
- uvicorn 0.54.0
- cryptography 50.0.1（Ed25519）
- pytest 9.1.1 / httpx 0.28.1（测试）
- SQLite 为 Python 标准库 `sqlite3`；配置解析为标准库 `tomllib`。
