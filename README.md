# safeproxy —— 本地出站目标校验内核（SSRF 防护演示）

一个**只访问本机测试服务**的出站代理安全内核演示。它回答的核心问题是：

> 给定一个可能被攻击者控制的 URL，在 URL 解析、主机规范化、DNS 解析和最终
> 连接这四个环节用**同一套策略**校验，能否保证被禁地址**从未被连接**，并
> 输出可复核的**决策链**？

技术栈：Python 3.12 · FastAPI · SQLite · cryptography（Ed25519）。
全部外部参与者（DNS、源站）均为本地合成夹具，**不需要公网，也不向真实内网
发起任何探测**。

---

## 它防住了什么

| 攻击/异常 | 夹具/输入 | 结果 |
|---|---|---|
| 云元数据直连 | `http://169.254.169.254/` | `policy_deny / E_POLICY_DENY`，不连接 |
| DNS 重绑定（TOC-TOU） | `rebind.example` → `{127.0.0.1, 169.254.169.254}` | 混合集整组拒绝，**连集合里允许的那个也不连** |
| IPv4-mapped IPv6 | `http://[::ffff:169.254.169.254]/` | 解包为元数据地址后拒绝 |
| 用户名片段混淆 | `http://alice@169.254.169.254/` | 输入面拒绝，不静默剥离 userinfo |
| 整数/八进制/十六进制 IP | `http://2130706433/`、`0x7f000001`、`0177.0.0.1` | 先规范化成点分十进制再判定 |
| 重定向到内网 | 允许页 `302 → 元数据` | 第一跳允许并连接，**第二跳重校验后拒绝、不连接** |
| 重定向环 | `/loop-a ↔ /loop-b` | `state_conflict / E_REDIRECT_LOOP` |
| 重定向预算耗尽 | `/many → /many` | `resource_exhausted / E_REDIRECT_BUDGET` |
| 非法 scheme / 端口 / 编码 | `gopher://`、`:70000`、`%2f` | `input_error / E_*` |

五类可预期失败彼此可区分：`input_error`、`policy_deny`、`state_conflict`、
`resource_exhausted`、`computation_failed`，并分别映射到 HTTP 400/403/409/429/502。

---

## 四条安全铁律（实现位置）

1. **同一策略口径** —— URL 主机、DNS 答案、最终连接地址都先经过
   `net/addrip.py` 规范化，再由 `rules/policy.py` 的同一个引擎判定。
2. **重定向每跳重校验** —— `kernel.py` 把每个 `Location` 当全新 URL，从
   parse 开始重走全部检查；上一跳允许不传导到下一跳。
3. **固定解析结果连接** —— `net/connector.py` 只接收内核选定的
   `IpCandidate`（IP 字面量）直接 `socket.connect((ip, port))`，**绝不让
   socket 再解析主机名**；连接后用 `getpeername()` 复核对端，不一致即
   `E_PIN_MISMATCH`。
4. **混合集 `deny_if_any_forbidden`** —— 一次解析返回多个地址时，只要有
   一个被禁，整组拒绝、**任何候选都不连接**，逐候选判定写入决策链。

---

## 目录结构

```
src/safeproxy/
  errors.py            错误分类法（跨模块错误契约）
  contracts.py         不可变数据契约（ParsedUrl/IpCandidate/HopDecision…）
  kernel.py            安全内核：分阶段裁决 + 每跳重校验 + 决策链
  net/
    addrip.py          IP 字面量规范化（mapped 解包、整数/进制收敛）
    urlparse.py        严格 URL 解析与主机规范化、重定向合并
    resolver.py        受控 DNS 夹具：合成 zone + 可回放/轮转解析器
    connector.py       固定 IP 直连器 + 对端复核（不二次解析）
  rules/
    loader.py          规则/证据文件解析（严格 schema、插值）
    policy.py          策略引擎：CIDR/host、混合集、敏感度
  audit/store.py       SQLite 哈希链 + Ed25519 签名审计
  service/
    origin.py          仅环回的本机测试源站（正常页+各类重定向）
    api.py             FastAPI 边界（/v1/fetch、/v1/audit/*）
fixtures/
  policy.json          出站策略（默认拒绝 + 唯一放行本机源站端口）
  dns/primary.zone     受控 DNS 夹具（重绑定/mapped/元数据…）
  expected.json        独立测试预言（手写预期，非内核生成）
tests/                 86 个测试（独立预言/重绑定/重定向/pinning/审计/E2E）
scripts/               demo.py、serve.py、curl_examples.sh
docs/REPRODUCE.md      复现指南与失败重放说明
```

模块间只传递 `contracts.py` 里的不可变对象，错误只抛 `errors.py` 里的类型；
没有为凑文件数制造空壳，也没有把全部逻辑塞进一个文件。

---

## 快速开始

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.lock          # 或 pip install -e .[test]

# 1) 一键演示（启动本机源站，跑正常+攻击场景，打印决策链）
python scripts/demo.py --jsonl

# 2) 跑测试
python -m pytest -q

# 3) 启动服务并执行 curl 调用示例（另开一个终端）
python scripts/serve.py --port 8080
bash scripts/curl_examples.sh
```

## HTTP 接口

| 方法与路径 | 作用 |
|---|---|
| `POST /v1/fetch` `{"url": ...}` | 发起受保护抓取；成功 200，失败按类别返回 4xx/5xx，body 始终含 `run_id` 与决策链 |
| `GET /v1/audit/runs?verdict=&limit=` | 列出运行 |
| `GET /v1/audit/runs/{run_id}` | 取一次运行的完整记录（含每跳） |
| `GET /v1/audit/verify` | 重算哈希链并校验 Ed25519 签名 |

## 测试为何可信（参考答案独立）

`fixtures/expected.json` 的预期 verdict/code/规则全部**手写**；
`tests/test_oracle.py` 再用**标准库 `ipaddress`** 独立重算地址分类。于是有
三个相互独立的来源交叉比对：手写预言、标准库分类、被测内核决策。内核的
输出从不被用来生成"标准答案"。被禁地址"从未被连接"由 `RecordingConnector`
记录的实际 connect 对端集合直接断言。
