# deletable-cuckoo

支持**受控删除**的 Cuckoo 近似成员过滤器服务（Rust + Axum + 本地文件系统）。

成员判定是概率性的：**可能假阳性，但对已插入且存活的键保证不假阴性**。删除不是
「凭过滤器说在就删」，而是必须出示插入时签发的**删除凭证（capability token）**，
因此删除假阳性不会误伤其他键。

- 语言/框架：Rust 2021 / Axum 0.7 / Tokio，纯本地合成夹具，无外部账号依赖。
- 指纹与两个候选桶的计算**固定、确定、带版本**，另有独立 Python 预言机交叉核对。
- 迁移（kick）次数有上限；插入失败整体**逐字节回滚**。
- 每个桶槽存 `(指纹, 拥有者 key_id, 副本数)`，跨键不去重；删除按凭证中的拥有者精确匹配。
- 插入/删除与单文件原子快照在同一事务内：落盘失败则内存一并回滚。

---

## 1. 构建

```bash
cargo build --release            # 产物：target/release/cf-svc
cargo test                       # 运行全部 43 项测试
```

关键依赖已用精确版本锁定并提交 `Cargo.lock`（axum 0.7.9、tokio 1.53.1、
sha2 0.10.9、hmac 0.12.1、serde 1.0.229 等，共 115 个 crate 固定）。

运行：

```bash
./target/release/cf-svc config/default.toml
# 不传参数时默认读取 config/default.toml
```

---

## 2. 快速示例（curl）

```bash
# 插入，得到删除凭证 delete_token
curl -s -X POST 127.0.0.1:8080/filter/insert \
  -H 'Content-Type: application/json' \
  -d '{"key":"alice@example.com"}'

# 成员查询
curl -s -X POST 127.0.0.1:8080/filter/contains \
  -H 'Content-Type: application/json' -d '{"key":"alice@example.com"}'
# {"ok":true,"member":true}

# 删除（把上一步的 delete_token 填入）
curl -s -X POST 127.0.0.1:8080/filter/delete \
  -H 'Content-Type: application/json' \
  -d '{"delete_token":"<DELETE_TOKEN>"}'
```

可直接运行 `examples/demo.sh`（自动选择端口、插/查/删/重放/统计）。

---

## 3. HTTP 接口

所有请求/响应均为 JSON（除 `GET /healthz`、`GET /stats`）。每个响应回显
`x-request-id`（可用同名请求头传入自定义关联 ID）与 `x-run-id`（运行身份）。
**错误绝不返回成功**：失败带确定的 HTTP 状态码与 `error.code`。

| 方法 & 路径 | 说明 |
|---|---|
| `GET /healthz` | 版本、内核/快照/令牌版本、run_id |
| `GET /stats` | 占用、负载、存活键/副本数、累计计数 |
| `POST /filter/insert` | `{ "key": "..." }` → `delete_token` |
| `POST /filter/contains` | `{ "key": "..." }` → `{ "member": bool }` |
| `POST /filter/delete` | `{ "delete_token": "..." }` |

### 错误码

| code | HTTP | 含义（失败类别） |
|---|---|---|
| `invalid_request` / `key_empty` / `key_too_large` | 400 | 请求体/键非法 |
| `token_malformed` / `token_unsupported_version` | 400 | 令牌编码损坏 |
| `token_bad_signature` | 403 | 伪造或篡改 |
| `token_unknown_key` | 403 | 键无有效插入记录 |
| `token_ordinal_never_issued` | 403 | 序号越界 |
| `token_replayed` | 403 | 令牌已使用，禁止重放 |
| `filter_full` / `duplicate_limit` | 503 | 容量耗尽（插入已回滚）/ 副本上限 |
| `internal_invariant` / `persistence_failed` | 500 | 不变式破坏 / 落盘失败（事务已回滚） |
| `not_found` | 404 | 未知路由 |

### 插入语义（重复键计数）

- 同一键第 1 次插入：`newly_occupied=true, duplicate=false, live_count=1`，新占一个槽。
- 同一键再次插入：`duplicate=true`，**只增加该槽副本数，不重复占槽**；每次仍签发
  一张序号唯一的新凭证。
- 删除一张凭证递减一份副本；副本归零才清空槽位。因此「插 N 次需删 N 次」。

---

## 4. 正确性设计（审查重点）

### 4.1 固定的指纹与两个候选桶

见 `src/hashing.rs`，全部为域分离的 SHA-256（小端、长度前缀）：

```
H_index(key) 决定 i1；fp = 1 + H_fp(key) mod (2^f - 1)   （0 保留给空槽）
i2 = (i1 XOR H_alt(fp)) & (m-1)
```

`H_alt` **只依赖指纹、不含当前桶号**，所以 `i XOR h(fp)` 是对合（involution）：
从 i2 能精确反推 i1——这是删除与迁移能找到另一个桶的前提。口径随 `kernel_version`
版本化；`tests/golden/` 下的独立 Python 预言机用 OpenSSL 的 SHA-256 重新实现同一口径
并生成黄金向量，Rust 测试只读取、不生成期望值。

### 4.2 迁移上限与失败回滚

插入先找两个候选桶空槽；满了则随机迁移，最多 `max_kicks` 次。每次迁移记录
`(桶, 槽, 迁移前完整单元)`；达上限即沿迁移链**反向交换**，逐字节恢复调用前状态，
再返回明确的 `filter_full`（不与成功混用）。

### 4.3 为什么删除不会误伤其他键（无正常数据假阴性）

标准 Cuckoo 过滤器按指纹去重，两个不同键指纹碰撞且候选桶相交时会共用槽，删除一个
会连坐另一个。本实现每个槽是：

```
Cell { fingerprint: u32, copies: u32, owner: [u8;32] }
```

- 不同 `owner` 即使指纹相同也**各占独立槽位**；
- 删除只接受签名凭证，并在桶内按 `(指纹, owner)` 精确匹配后递减，绝不触碰他人槽位；
- 记账层（`src/ledger.rs`）为每个键维护「已签发数 / 已花费序号位图」，序号唯一、
  不可重放，存活副本数精确。

因此只要记账显示某键仍存活，其指纹单元必在两个候选桶之一，`contains` 不可能为假。

### 4.4 凭证（capability token）

`src/token.rs`：载荷 = 版本 + key_id(32B) + 插入序号 + i1 + i2 + 指纹，
追加 HMAC-SHA256（服务端密钥，`<data_dir>/secret.key`，0600），base64url 输出。
无密钥者无法为任意键伪造凭证；任何载荷篡改都会使签名失效。

### 4.5 持久化事务与完整性

`src/persistence.rs`：单文件快照 = 头部（含内核参数、计数）+ 头部 CRC-32 +
ledger JSON + 定长单元区 + 整体 SHA-256。写入走「临时文件 → fsync → rename →
fsync 目录」的原子流程。加载时严格校验：magic/版本、头部 CRC、整体 SHA、参数与
当前配置一致、长度自洽、以及「记账存活总数 == 桶表副本总数」交叉不变式；任何一项
不符即拒绝启动。插入/删除只有在快照落盘成功后才对外成功，否则内存随事务回滚。

---

## 5. 配置

见 `config/default.toml`，逐项有注释。环境变量可覆盖：`CF_HOST`、`CF_PORT`、
`CF_DATA_DIR`、`CF_FSYNC`、`CF_HMAC_SECRET_HEX`、`CF_LOG_LEVEL`、`CF_RUN_ID`、
`CF_FILTER_SEED_HEX`。非法参数（桶数非 2 的幂、指纹位宽越界、全零/长度错误的种子、
过大迁移上限等）在启动期即报错。

生产注意：请用环境变量覆盖合成夹具中的固定种子与 HMAC 密钥；密钥丢失会导致旧删除
凭证全部失效。

---

## 6. 测试

```bash
# 全部测试（内核单元 + 独立集成测试 + 真实 HTTP 端到端）
cargo test

# 用独立语言的预言机重新生成黄金向量（期望值不来自被测 Rust 代码）
python3 tests/golden/golden_oracle.py
```

测试组织（均为**独立断言具体结果/失败类别**，不是只验证「接口可调」）：

| 文件 | 覆盖 |
|---|---|
| `tests/kernel_golden.rs` | 逐向量比对 Python 预言机的 i1/fp/i2、对合性、确定性 |
| `tests/core_properties.rs` | 小桶强制迁移环、失败**逐字节**回滚、重复计数、跨 owner 指纹碰撞隔离、混合插删后零假阴性 |
| `tests/false_positive_rate.rs` | 固定种子实测 FPR 并与理论量级比较（8 位 ≈1.54%、16 位 ≈1.2e-4） |
| `tests/service_transactions.rs` | 落盘失败回滚、快照往返、凭证各类失败、重启后令牌有效 |
| `tests/snapshot_integrity.rs` | 截断/单元区篡改/头部参数篡改/参数不匹配/损坏文件拒绝启动 |
| `tests/http_e2e.rs` | 真实 TCP 起服务：插查删、重放 403、关联头、坏请求错误码、重启保状态 |
| `tests/config_validation.rs` | 各类非法配置在启动期被拒 |

日志默认文本格式（可切 JSON），事件带 `run_id`、`request_id`、键标识、版本、
迁移步数、判定依据（如容量耗尽时的占用与 attempted_kicks）。FPR 测试会打印实测值
与理论值。

---

## 7. 数据与目录

```
<data_dir>/
  secret.key     # HMAC 密钥（自动生成，0600）
  snapshot.bin   # 原子快照；另有写入中的 snapshot.bin.tmp
```

快照格式见 `src/persistence.rs` 顶部布局表。

---

## 8. 已知限制（如实说明）

1. **单进程、全局互斥**：所有修改串行化并同步落盘，强调正确性而非吞吐；多副本部署
   不在范围内。
2. **容量受负载因子约束**：b=4、16 位指纹建议负载不超过约 0.95，超过会更早出现
   `filter_full`（此时已插入数据不受影响、无假阴性，仅新插入被拒）。扩容需要新建
   更大过滤器并重新插入（当前版本不在线 rehash）。
3. **假阳性随指纹宽度变化**：8 位指纹仅用于放大假阳性做测试，生产请用 16 位及以上。
4. 快照不加密（本机文件权限保护）；密钥与快照同机，威胁模型假定本地文件系统可信。
5. 删除凭证是**持有者令牌**：泄露令牌即等于授予一次删除，请走保密通道交付。
6. 成员查询只暴露布尔结果；精确存活副本数仅在 `/stats` 聚合与服务内部使用。
