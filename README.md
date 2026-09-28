# SSRF 出站目标校验内核 —— 复现文档

本项目实现一个**本地代理出站目标校验内核**，并演示“仅访问本机测试服务”。
全程离线：DNS 用受控夹具应答，连接目标只可能是 `127.0.0.1`，
不通过公网实测内网探测。

## 1. 它回答的具体问题

| 攻击/异常夹具 | 预期最终裁决 | 失败类别 + 原因码 | 被禁地址是否被连接 |
|---|---|---|---|
| DNS 重绑定（首帧公网/次帧元数据） | 每跳独立快照，第二帧 `169.254.169.254` 被拒 | `policy_denied / ip.blocked` | **0 次** |
| 首帧即元数据的重绑定 | deny | `policy_denied / ip.blocked` | **0 次** |
| 一次应答混合 {可放行, 内网} | 整跳拒绝 | `policy_denied / ip.mixed_candidates` | **0 次** |
| IPv4-mapped IPv6 `[::ffff:169.254.169.254]` | 解包成 IPv4 后按 link-local 拒绝 | `policy_denied / ip.blocked` | **0 次** |
| URL 用户名片段 `http://user@host/`、`http://169.254.169.254@host/` | deny | `input_error / userinfo.forbidden` | **0 次**（DNS 都不触发） |
| 整数/八进制 IP `2130706433`、`0177.0.0.1` | deny | `input_error / host.integer_ip`、`host.ambiguous_numeric` | 0 次 |
| 重定向环 A→B→A | deny | `state_conflict / redirect.loop` | 第三跳发包前拦截 |
| 重定向到 `file://` | deny | `input_error / redirect.scheme_unsupported` | 第二跳不连接 |
| 重定向到元数据/重绑定主机 | deny | `policy_denied / ip.blocked` | 仅第一跳连 127.0.0.1 |
| 重定向跳数超限 | deny | `resource_exhausted / redirect.limit` | 恰好 6 次请求（上限 5） |
| NXDOMAIN / DNS 临时故障 | deny | `computation_failed / dns.name_not_found`、`dns.temporary` | 0 次 |
| 连接被拒绝 | deny | `computation_failed / connect.refused` | 1 次尝试（可审计） |
| 响应体超上限 | deny | `resource_exhausted / response.too_large` | 1 次 |
| 对端 IP 与固定地址不符 | deny | `state_conflict / connect.pin_mismatch` | — |
| 正常访问本机 `/ok`、`/redirect→/ok`、HTTPS | **allow** | — | 固定连接 `127.0.0.1` |

每次 run 都输出带序号的**决策链**（`run_start → url_parse → dns_resolve →
ip_classify → policy → connect → request_sent → response_read → redirect → finish`），
以及逐地址规则命中、pinning IP、对端 IP、连接尝试清单。

## 2. 目录结构（模块边界与数据契约）

```
app/
  contracts.py   跨模块数据/错误契约：FailureKind、Reason、ParsedTarget、
                 ResolvedAddress/ResolvedTarget、Evidence、HopRecord、RunResult
  urlparse.py    受控 URL 解析与主机规范化（IDNA、userinfo、整数 IP、端口、fragment）
  ipclass.py     IP 字面量分类事实来源（显式特殊网段；mapped/compatible IPv6 解包）
  policy.py      有序规则/grant 解析与求值（首条命中；混合候选集规则）
  resolver.py    受控 DNS 夹具（records / script 重绑定脚本 / error；克隆隔离）
  connector.py   仅连数字 IP 的固定连接原语 + 对端校验 + RecordingConnector
  httpclient.py  在固定 socket 上的最小 HTTP/1.1（响应体上限、chunked、Location）
  kernel.py      安全内核：统一策略编排、每跳重校验、证据链、运行编号
  audit.py       SQLite(WAL)+JSONL 审计，Ed25519 证据签名与校验
  pki.py         本地合成 CA/服务证书（cryptography，无外部账号）
  demo_upstream.py 仅绑定 127.0.0.1 的演示上游（ok/redirect/loop/chain/large/...）
  webapi.py      FastAPI：/v1/fetch、/v1/runs、/v1/runs/{id}/verify、/v1/key
scripts/
  run_demo.py    一键端到端演示（可 --web / --https）
  replay_run.py  按 run_id 从审计库重放、验签、打印关键中间状态
fixtures/
  policy/rules.json           静态有序策略（显式 deny + 默认 deny）
  dns/zones.json              受控 DNS 区域（重绑定脚本、mapped、NXDOMAIN...）
  expected/expected_results.json  手工编写的独立参考答案（oracle）
tests/                         124 个测试（具体结果/失败类别，非“能调用”）
reports/                       保留的一次真实运行控制台与 run 摘要
state/                         运行期 SQLite/JSONL/PKI（默认不提交，演示生成）
```

模块间只通过 `app/contracts.py` 中的 dataclass 与异常通信；
错误被严格区分为四类（+ 原因码）：

* `input_error`（调用方输入非法）
* `policy_denied`（输入合法但策略禁止；对应 HTTP 403）
* `state_conflict`（运行期状态冲突：环、pin 不符；409）
* `resource_exhausted`（跳数/响应体/超时预算；508）
* `computation_failed`（DNS/连接/TLS/协议等；502）

## 3. 安装与锁定

需要 Python 3.12（3.10+ 亦可）。直接依赖见 `requirements.txt`，
完整传递锁定（含哈希生成时的版本）见 `requirements.lock`。

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt          # 或 pip install -r requirements.lock
```

## 4. 运行测试

```bash
. .venv/bin/activate
python -m pytest -q                       # 全部 124 个测试（约 1 秒，离线）
python -m pytest tests/test_kernel.py -q  # 仅内核攻击夹具集成测试
python -m pytest -k rebind -q             # 只跑重绑定相关
```

## 5. 端到端演示（真实运行的正常与异常路径）

```bash
. .venv/bin/activate

# 控制台：12 个场景 + 完整决策链（结果示例见 reports/demo_run_console.txt）
SSRF_GUARD_STATE=state/demo python -m scripts.run_demo

# 额外启用 FastAPI（打印 curl 示例）
SSRF_GUARD_STATE=state/web python -m scripts.run_demo --web --web-port 8088

# 本机合成 TLS（EC P-256 CA + 含 SAN 的服务证书）
SSRF_GUARD_STATE=state/tls python -m scripts.run_demo --https
```

## 6. 服务调用示例

启动 `--web` 后（`$PORT` 是演示上游端口，见启动日志）：

```bash
# 健康检查
curl -s http://127.0.0.1:8088/health

# 精确 per-run grant 访问本机服务（records 把 demo.local 固定到 127.0.0.1）
curl -s -XPOST http://127.0.0.1:8088/v1/fetch \
  -H 'content-type: application/json' \
  -d '{"url":"http://demo.local:'"$PORT"'/ok",
       "grants":[{"id":"g-demo","host":"demo.local","scheme":"http",
                  "port":'"$PORT"',"records":["127.0.0.1"]}]}'

# 攻击：云元数据 -> 403，body 内含完整决策链；元数据地址连接 0 次
curl -s -XPOST http://127.0.0.1:8088/v1/fetch \
  -H 'content-type: application/json' \
  -d '{"url":"http://169.254.169.254/latest/meta-data/"}'

# 审计与重放
curl -s 'http://127.0.0.1:8088/v1/runs?limit=20'
curl -s http://127.0.0.1:8088/v1/runs/<run_id> | python -m json.tool
curl -s http://127.0.0.1:8088/v1/runs/<run_id>/verify
curl -s http://127.0.0.1:8088/v1/key        # Ed25519 公钥（PEM）

# 离线从审计库重放（不依赖服务仍在运行）
python -m scripts.replay_run state/demo/audit --last
python -m scripts.replay_run state/demo/audit <run_id> --json
```

HTTP 状态码映射：`input_error=400, policy_denied=403,
state_conflict=409, resource_exhausted=508, computation_failed=502`。
**被拒绝时响应体仍携带完整决策链**，便于复核。

## 7. 安全设计要点（与题目要求一一对应）

1. **URL 解析、主机规范化、DNS、最终连接地址同一口径**
   规范化主机经 `ipclass` 分类；DNS 候选走同一分类器；连接器只接受
   策略快照中的数字 IP，内部不调用 `getaddrinfo`。
2. **重定向每跳重校验**：每个 `Location` 重新解析/分类/求值/连接；
   允许与禁止混合集合规则明确——**逐地址求值，全部 allow 才连接，
   任一 deny 即整跳 `ip.mixed_candidates`，且不连接任何候选**。
3. **固定解析结果连接（pinning）**：一跳恰好解析一次，快照即连接依据；
   连接后用 `getpeername()` 与固定 IP 逐字节比对，不一致即
   `state_conflict/connect.pin_mismatch`。DNS 脚本的第二帧在同跳无人消费。
4. **不通过公网实测内网**：无真实 DNS；`records` 全是文档地址或
   `127.0.0.1`；唯一真实监听的是本机演示上游。
5. **IPv4-mapped/compatible IPv6 解包**：`::ffff:169.254.169.254`
   与 `169.254.169.254` 得到同一标签集；`::1` 保留 IPv6 loopback 语义。
6. **状态隔离**：每次请求克隆 resolver（重绑定脚本计数不跨 run），
   grant 只存在于当次策略副本；RecordingConnector 每 fetch 重置。
7. **可重放/可复核**：run_id（UTC 时间戳 + 单调序号）、证据链、
   逐跳中间状态、失败原因全部落 SQLite + JSONL；规范 JSON 经 Ed25519
   签名，篡改后 `/verify` 返回 `valid=false`（有测试覆盖）。

## 8. 独立参考答案（oracle）的独立性

`fixtures/expected/expected_results.json` 由测试作者**手工**编写，
不是从 `app/` 实现导出生成；文件头 `_provenance` 声明了这一点。
`test_oracle_expected_results_match_behavior` 用它交叉核对真实行为。
另外大量期望值（地址段标签表、拒绝原因码、连接计数）直接内联在
测试中，与被测实现无共享代码路径。

## 9. 复现失败时看什么

* 控制台 / `reports/demo_run_console.txt`：每个场景的 `run_id`、
  verdict、failure `[kind] reason`、逐阶段证据、connections 清单。
* `state/<run>/audit/events.jsonl`：begin/finish 原始事件（含重放所需全部字段）。
* `state/<run>/audit/audit.sqlite3`：`runs`（终态+签名）、`events` 两表。
* `scripts/replay_run.py`：打印每跳 host/DNS 候选/规则命中/连接尝试。
* 失败类别可区分：输入错误（400）、策略拒绝（403）、状态冲突（409）、
  资源耗尽（508）、计算失败（502），且原因码细到具体子情形。
