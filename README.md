# Cache-Key & Vary 配置审计后端

对**本地合成**的响应元数据进行审计：给定一条声明式缓存键策略和一组请求/响应证据，
判定“不同请求同键但响应不同”的碰撞，并返回碰撞请求对见证（witness）。

技术栈：Python 3.10+ · FastAPI · SQLite · cryptography。无外部账号、无真实业务数据，
所有参与者（凭据、压缩体、身份响应）都在 `fixtures/` 里本地合成。

## 它审计什么

1. **请求维度覆盖**：策略声明缓存键包含哪些维度（`path`、`query`、
   `accept-language`、`accept-encoding`、`authorization`、`cookie` 等）。
   键只对这些维度做 HMAC；两个请求在未覆盖维度上产生了实质不同的响应，就是
   `collision`，并给出具体请求对与未覆盖维度。
2. **Authorization / Cookie 不允许隐式跨身份复用**：
   - `identity.mode = auto`（默认）：携带凭据的请求即使策略没声明身份维度，
     内核也强制按身份隔离键（fail-safe），并报 `implicit_identity_keying`；
   - `identity.mode = shared`：策略显式声称可跨身份共享，此时跨身份复用会作为
     `critical` 的 `collision` + `identity_shared` 证据返回；
   - `identity.mode = per_identity`：正确声明，按身份打键。
   键和数据库里只保存凭据的 HMAC 标签，原始 token 永不落盘。
3. **Vary 通配与缺失头的区别**：
   - `Vary: *` → `vary_wildcard`（critical）。通配响应永不进入共享分组——
     增加任何键维度都无法“修复”它；
   - Vary 头缺失 → `missing_vary`，理由明确写“no Vary header at all”；
   - Vary 存在但没列出导致差异的维度 → 同样 `missing_vary`，但理由是
     “does not list it”，与完全缺失区分；
   - Vary 列出了键未覆盖的已知维度 → `uncovered_vary_dimension`；
   - Vary 列出审计器无法建模的头（如 `x-custom`）→ `unmodelled_vary_dimension`，
     只说“无法验证覆盖”，不臆测业务隐私。
4. **私有缓存**：`Cache-Control: private|no-store` 的响应出现在共享策略中 →
   `private_in_shared_cache`（critical）。

系统只判断“给定策略是否覆盖证据中观察到的差异”，不声称自动理解任意业务隐私。

## 反例 → 修复键 → 碰撞消失

`fixtures/` 提供四类核心反例：

| 夹具 | 场景 | 弱策略（path+query）下 |
|---|---|---|
| `language.json` | 语言协商 `Accept-Language: zh-CN/en` | 碰撞 `r-zh` ↔ `r-en`，未覆盖 `accept-language` |
| `encoding.json` | 压缩编码 `Accept-Encoding: gzip/br` | 碰撞 `r-gzip` ↔ `r-br`，未覆盖 `accept-encoding` |
| `identity.json` | 不同 Bearer token 的 `/account` 私有响应 | `shared` 模式下 critical 碰撞 `r-alice` ↔ `r-bob` |
| `missing_vary.json` | 语言差异但响应完全没有 Vary | 碰撞 + `missing_vary` |
| `vary_wildcard.json` | `Vary: *` | 不进共享分组，critical `vary_wildcard` |

修复键（加入 `accept-language`、`accept-encoding`、`authorization`、`cookie` 等）后
所有 `collision` 归零；但 `missing_vary` 等起源端缺陷仍会报告——审计器不会假装
Vary 头存在。

## 模块边界与数据/错误契约

```
app/
  errors.py    错误分类法：input / state / resource / computation
  models.py    不可变数据契约：RequestMeta / ResponseMeta / CachePolicy / Finding / Witness
  parsing.py   规则（策略）与证据解析；结构问题 -> InputError
  kernel.py    安全内核：HMAC 打键、Vary 分析、碰撞检测（纯计算，无 I/O）
  store.py     SQLite 状态；按 run_id 隔离，created -> finalized 生命周期
  service.py   编排：解析 -> 限额 -> 内核 -> 持久化 -> Ed25519 签名报告
  api.py       FastAPI，错误分类到 HTTP 状态码
fixtures/      五组合成本地证据
scripts/demo.py  端到端演示（真实 ASGI 应用 + 临时 SQLite）
tests/         pytest，40+ 用例
```

### 错误语义（四类必须可区分）

| category | HTTP | 典型 code | 含义 |
|---|---|---|---|
| `input` | 400 | `policy.dimensions_missing` / `policy.unknown_dimension` / `evidence.dangling_response` / `evidence.no_source` / `fixture.not_found` | 客户端策略或证据结构错误 |
| `state` | 409 / 404 | `run.duplicate` / `run.finalized` / `run.not_found` / `request.duplicate` | 与已持久化状态冲突（重复 run、终态写入、未知 run） |
| `resource` | 429 | `limit.requests` / `limit.findings` | 超出策略配置的请求数/发现数上限 |
| `computation` | 500 | `compute.header_type` / `compute.unmatched_request` / `compute.secret` / `verify.key_mismatch` | 结构合法但无法完成计算（如头值不是字符串、内部不变量被破坏） |

错误响应统一形如：

```json
{"error": {"category": "input", "code": "policy.unknown_dimension",
           "message": "...", "detail": {"unknown": ["x-telepathy"]}}}
```

### Finding 种类（闭集）

`collision`、`missing_vary`、`vary_wildcard`、`uncovered_vary_dimension`、
`unmodelled_vary_dimension`、`private_in_shared_cache`、`implicit_identity_keying`、
`identity_shared`；严重级 `info` / `warning` / `critical`。

### 可重放性

- 每个审计有 `run_id`；每 run 的 HMAC 密钥由本地主密钥经 HKDF(salt 固定,
  info=`run:<id>`) 派生，主密钥保存在 `var/`（内存模式下临时生成），run 密钥不落盘；
- 每次审计落库 run 元数据、原始请求、完整事件流（`run_started`、`request_keyed`
  含键前缀/是否身份隔离、`group_formed`、`pair_compared`、`finding_emitted` 含理由、
  `run_finished`），可通过 `GET /audits/{id}/events` 重放判断过程；
- 报告用本地 Ed25519 密钥签名，`GET /audits/{id}/verify` 可离线验证，篡改报告后校验失败。

## HTTP 接口

| 方法 路径 | 说明 |
|---|---|
| `GET /health` | 健康检查 + 签名密钥 id |
| `POST /policies/validate` | 只校验策略 |
| `POST /audits` | 执行审计，body：`{policy, evidence? | fixture?, run_id?}`，201 返回签名报告 |
| `GET /audits/{run_id}` | 取报告（含碰撞见证） |
| `GET /audits/{run_id}/events` | 取可重放事件日志 |
| `GET /audits/{run_id}/verify` | 验证报告签名 |

策略示例：

```json
{
  "name": "full",
  "covered_dimensions": ["path", "query", "accept-language",
                         "accept-encoding", "authorization", "cookie"],
  "identity": {"mode": "per_identity"},
  "shared": true,
  "limits": {"max_requests": 1000, "max_findings": 500}
}
```

## 运行步骤

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 1) 测试（实际执行，报告具体结果）
.venv/bin/python -m pytest

# 2) 端到端演示：构造反例 -> 修复键 -> 碰撞消失 -> 签名验证 -> 四类错误
.venv/bin/python scripts/demo.py

# 3) 启动 HTTP 服务
.venv/bin/uvicorn app.api:app --host 127.0.0.1 --port 8000
# 然后例如：
curl -s 127.0.0.1:8000/audits -H 'content-type: application/json' \
  -d '{"policy":{"name":"p","covered_dimensions":["path","query"]},
       "fixture":"language"}' | python3 -m json.tool
```

## 测试如何给出独立参考答案

- 期望键**不**由被测内核生成：测试用标准库 `hmac`/`hashlib` 对手写规范消息
  独立重算（`tests/test_kernel.py::test_key_matches_independent_stdlib_derivation`）；
- 碰撞断言具体的请求 id、键前缀、未覆盖维度与严重级，而不是“接口能调用”；
- 断言具体错误 category/code（400/409/429/500 各自的失败类别）；
- 断言凭证原文不落库、修复键后碰撞数为 0、篡改报告后签名校验失败。
