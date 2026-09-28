# 复现与失败重放指南

本文说明如何从零复现正常与异常结果，以及当某条抓取"失败/被拒"时，如何用
**运行编号（run_id）+ 关键中间状态 + 判断理由**重放问题。

## 1. 环境与依赖锁定

```bash
python3 --version          # 需要 3.12（在 3.12.3 上验证）
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.lock
```

`requirements.lock` 是 `pip freeze` 生成的精确版本锁（fastapi 0.115.6 /
uvicorn 0.34.0 / httpx 0.28.1 / cryptography 44.0.0 / pydantic 2.10.4 /
pytest 8.3.4 等）。安装只用于本地运行，运行期不访问公网。

## 2. 一键复现

```bash
python scripts/demo.py --jsonl
```

它会：

1. 在 `127.0.0.1:18080` 启动**仅绑定环回**的本机测试源站；
2. 用受控 DNS 夹具（`fixtures/dns/primary.zone`）装配真实安全内核；
3. 依次跑 2 个正常场景和 9 个攻击/异常场景；
4. 每个场景打印 `run_id`、结论、失败类别和完整决策链；
5. 把结构化结果写到 `artifacts/demo.jsonl`，并重算审计哈希链验证；
6. 退出码：正常场景全 allow 且攻击/异常场景全非 allow 时为 `0`。

最近一次真实运行结果：

```
审计链验证： ok=True  记录数=12
演示自检通过：正常场景全部 allow，所有攻击/异常场景均未放行。   (exit 0)
```

跑测试：

```bash
python -m pytest -q          # 86 passed
```

## 3. 决策链长什么样

每条抓取按阶段留痕，单跳包含四阶段：

```
hop1 解URL   [allow] parsed            host_kind=domain
hop1 解析DNS [allow] dns:1th_resolution 候选=['127.0.0.1','169.254.169.254']
hop1 策略    [deny ] deny_if_any_forbidden:deny-metadata-ipv4
             · 被禁候选=['169.254.169.254']; 同组允许但【不会被连接】的候选=['127.0.0.1']
```

字段含义（`contracts.HopDecision`）：

| 字段 | 作用 |
|---|---|
| `hop` / `stage` | 第几跳、哪个阶段（parse/dns/policy/connect/redirect） |
| `verdict` / `reason` | 该阶段结论与机器可读理由 |
| `resolved` | 规范化后的**全部**候选 IP（不只是被选中的那个） |
| `matched.rule_id` | 命中的规则（混合集报告敏感度最高的禁因） |
| `peer_checked` | 连接后复核过的实际对端 |
| `location` | 本跳返回的重定向目标 |
| `note` | 人类可读判断理由（含"不会被连接"的候选） |

## 4. 失败类别（彼此可区分）

| category | code 示例 | HTTP | 语义 | 重试 |
|---|---|---|---|---|
| `input_error` | `E_SCHEME_FORBIDDEN` `E_HOST_BAD_CHAR` `E_PORT_RANGE` `E_USERINFO_*` | 400 | 输入本身不合法 | 否 |
| `policy_deny` | `E_POLICY_DENY` | 403 | 输入合法但命中出站策略 | 否 |
| `state_conflict` | `E_REDIRECT_LOOP` `E_AUDIT_CHAIN_BROKEN` `E_PIN_MISMATCH` | 409 | 与运行状态冲突 | 否 |
| `resource_exhausted` | `E_REDIRECT_BUDGET` `E_DNS_ANSWER_BUDGET` `E_TIME_BUDGET` | 429 | 预算耗尽 | 视情况 |
| `computation_failed` | `E_DNS_UNKNOWN_NAME` `E_CONNECT_FAILED` `E_HTTP_PROTOCOL` | 502 | 外部依赖/计算失败 | 是 |

重定向**环**与重定向**预算耗尽**的边界（在收到 Location、算出下一跳后判定）：

* 仍有跳数预算，但下一跳指向更早出现过的**不同** URL → `E_REDIRECT_LOOP`；
* 下一跳等于当前（自跳 A→A）视为"未推进"，继续消耗预算；
* 请求数用满"初始 + max_redirects"仍在被重定向 → `E_REDIRECT_BUDGET`。

夹具策略 `max_redirects=2`，因此 A↔B 在第 3 跳被识别为环，自跳 /many 用满
3 次请求被判预算耗尽——两个类别都能稳定复现。

## 5. 用 run_id 重放一次问题

每次抓取都有全局唯一 `run_id`（时间基 + PID + 自增序号），例如
`run-20260928T124949-1712027-000003`。

**方式 A：结构化日志**

```bash
grep <run_id> artifacts/demo.jsonl | python3 -m json.tool
```

里面有完整 `hops`、`error.category/code/details`、`pinned`、`connected_peer`、
`body_sha256`，足以离线复盘每个中间状态。

**方式 B：审计接口（服务运行中）**

```bash
python scripts/serve.py --port 8080        # 终端 1
curl http://127.0.0.1:8080/v1/audit/runs/<run_id> | python3 -m json.tool
curl http://127.0.0.1:8080/v1/audit/verify
```

**方式 C：直接查 SQLite**

```bash
sqlite3 artifacts/audit.sqlite3 \
  "SELECT verdict,error_code,error_cat FROM audit_runs WHERE run_id='<run_id>'"
```

审计是只增哈希链：`chain_n = SHA256(chain_{n-1} || canonical_json(record_n))`，
每段用 Ed25519 签名；签名私钥持久化在 `artifacts/audit.sqlite3.key`（0600），
因此跨进程也能验证。改动任意一条记录，`/v1/audit/verify` 会在第一条断链处
报 `E_AUDIT_CHAIN_BROKEN`（测试 `test_tamper_detected` / `test_swap_rows_detected`
确定性地验证了这一点）。

## 6. 受控 DNS：如何确定性复现重绑定

`fixtures/dns/primary.zone` 是人类可读的合成 zone：

```
rebind.example.  A  127.0.0.1
rebind.example.  A  169.254.169.254
```

`FixtureResolver` 每次调用都**轮转起点**并记录游标：

* 第 1 次解析返回 `(127.0.0.1, 169.254.169.254)`；
* 第 2 次返回 `(169.254.169.254, 127.0.0.1)`（模拟校验/连接两时点不同）。

关键在于内核在**集合层面**判定（`deny_if_any_forbidden`），而非只看排序第一
的答案，所以无论从哪个答案开始都拒绝，且一个候选都不连接。
`tests/test_rebinding.py::test_rebind_resolution_sequence_is_observable`
断言了这个确定的轮转序列。

## 7. "被禁地址从未被连接"是如何被证明的

* 单元/集成测试注入 `tests/conftest.RecordingConnector`，它记录每一次
  `connect((ip, port))` 的对端。策略拒绝发生在 connect 之前，因此测试直接
  断言被禁 IP 不出现在连接记录中（多个 `must_not_connect_any` /
  `attempts == []` 断言）。
* 真实路径 `PinnedHTTPConnector` 只拿 IP 字面量去 connect；
  `tests/test_pinning.py::test_connect_uses_ip_literal_not_name` 通过 monkeypatch
  拦截 `socket.connect`，断言其地址参数永远是已批准的 pin IP 而非主机名；
  `test_peer_mismatch_detected` 用谎报的 `getpeername()` 验证对端复核会抛
  `E_PIN_MISMATCH`。
* 全程没有任何用例去真实连接 169.254/10.x/192.168.x 等地址——这些只作为
  策略判定对象存在于夹具中，满足"不通过公网实际测试内网探测"。

## 8. 改动策略 / 增加用例

* 改 `fixtures/policy.json` 后，加载器会做严格 schema 校验；规则按文件顺序
  首条命中，`default_action` 固定为 `deny`。
* 新增攻击用例：在 `fixtures/dns/primary.zone` 加名字、在
  `fixtures/expected.json` 加一条**手写**预期（含 expected_verdict/code/rule
  与 why），测试会自动用标准库分类交叉核对。
* 删除/改名 `artifacts/audit.sqlite3*` 可得到一条全新的审计链与签名密钥。
