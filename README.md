# 可删除 Cuckoo 近似成员过滤器服务

一个用 Rust + Axum + 文件系统实现的 **Cuckoo Filter** 成员判定服务，支持：

- **受控删除**：删除只接受「插入时签发、与具体键绑定、一次性」的凭证（HMAC token），
  避免普通 Cuckoo Filter 因指纹假阳性而误删其他键的副本；
- **有界迁移 + 失败回滚**：插入迁移（kick）次数有上限，超限返回明确的
  `FILTER_FULL`，并把槽位、占用计数、归属表**整体回滚**到插入前；
- **明确的重复键语义**：同一键每插入一次就多一个副本与一张独立凭证，
  副本计数 = 成功插入次数 − 成功删除次数；
- **近似但单向可靠**：查询**可能假阳性，正常数据不会假阴性**；
- **崩溃安全持久化**：`临时文件 + fsync + rename` 原子快照，全文件 XXH64 校验。

> 范围说明：本服务面向本地/单机与合成数据场景，单实例、单写线程（互斥锁串行化），
> 不包含分布式复制、鉴权网关与 TLS（见文末「剩余限制」）。

---

## 1. 目录结构（按层组织，非单文件）

| 路径 | 职责 |
|---|---|
| `crates/cuckoo-core/` | **索引/编码内核**：参数、固定指纹与双候选桶计算、确定性迁移 RNG、带回滚的过滤器、删除凭证 HMAC。无 I/O、无锁 |
| `crates/cuckoo-persist/` | **持久化适配**：二进制快照格式 + 校验和、原子文件存储、把「内核 + 凭证→槽位归属表」整体落盘 |
| `crates/cuckoo-api/` | **验证接口**：Axum HTTP 服务、配置层、键编码、统一错误码、请求 ID 与结构化日志 |
| `crates/cuckoo-verify/` | **独立测试/参考答案**：自带一份独立 XXH64 实现、固定夹具、独立迁移参考模型、9 个断言具体值的场景 |
| `config/` | 配置层示例（TOML） |
| `scripts/demo.sh` | curl 端到端示例 |
| `docs/` | 设计与格式说明 |

---

## 2. 构建与运行

需要 Rust（已在 1.98.1 验证）。关键依赖版本在根 `Cargo.toml` 用 `=x.y.z` 锁定，
并提交了 `Cargo.lock`。

```bash
cargo build --release
```

准备主密钥（≥16 字节）与配置：

```bash
mkdir -p data
head -c 32 /dev/urandom | base64 > data/master.key   # 本地演示密钥
# 编辑 config/filter.toml 指向 data/master.key
```

启动（配置文件路径作为第一个参数）：

```bash
cargo run -p cuckoo-api -- config/filter.toml
# 或 release: ./target/release/cuckoo-api config/filter.toml
```

也可用环境变量覆盖（优先级：环境变量 > TOML > 默认）：
`CKF_BIND`、`CKF_BUCKETS_EXP`、`CKF_BUCKET_SIZE`、`CKF_FINGERPRINT_BITS`、
`CKF_MAX_KICKS`、`CKF_RNG_SEED`、`CKF_SNAPSHOT_PATH`、
`CKF_MASTER_KEY`（直接给密钥）或 `CKF_MASTER_KEY_FILE`。
日志等级用 `CKF_LOG`（如 `CKF_LOG=debug`）。

---

## 3. HTTP 接口

所有请求/响应为 JSON。响应头回显 `X-Request-Id`（可自带，便于关联日志）。

### `POST /v1/filter/insert`
```json
{ "key": "user-42", "key_encoding": "utf8" }
```
`key_encoding` 可选：`utf8`（默认）或 `base64url`（无填充，用于二进制键）。

成功 `200`：
```json
{ "ok": true,
  "credential": "<jti_b64url>.<hmac_b64url>",
  "detail": { "kicks": 0, "placed_bucket": 12, "placed_slot": 3,
              "load_factor": 0.000061, "generation": 1 } }
```
容量耗尽 `507`：`{"ok": false, "error_code": "FILTER_FULL", ...}`。
**凭证是删除该副本的唯一凭据，请妥善保存。**

### `POST /v1/filter/lookup`
```json
{ "key": "user-42" }
```
```json
{ "ok": true, "member": true, "observed_copies": 1,
  "note": "近似结果：可能假阳性；正常数据不会假阴性", "generation": 1 }
```

### `POST /v1/filter/delete`
```json
{ "key": "user-42", "credential": "<insert 返回的 credential>" }
```
成功 `200`（删除该凭证对应的**那一个**副本）。失败使用明确错误码，**不会返回成功**：

| HTTP | `error_code` | 含义 |
|---|---|---|
| 400 | `BAD_REQUEST` | 空键、未知编码、JSON 无法解析 |
| 403 | `INVALID_CREDENTIAL` | 凭证格式错 / 签名错 / 与键不匹配 / 非本服务主密钥签发 |
| 404 | `NOT_PRESENT` | 账本与槽位不一致等内部状态问题 |
| 409 | `CREDENTIAL_EXHAUSTED` | 凭证已被消费（重放），或跨重启重放 |
| 507 | `FILTER_FULL` | 迁移达到上限，插入已回滚 |
| 500 | `STORAGE_ERROR` / `INIT_ERROR` | 落盘失败（内存已回滚）/ 启动参数或快照问题 |

### `GET /health`、`GET /v1/filter/stats`
返回版本、运行身份、generation 与占用/负载/存活凭证数。

### curl 速览
```bash
curl -s localhost:8088/health
CRED=$(curl -s -XPOST localhost:8088/v1/filter/insert \
  -H 'content-type: application/json' -d '{"key":"user-42"}' | jq -r .credential)
curl -s -XPOST localhost:8088/v1/filter/lookup -d '{"key":"user-42"}'
curl -s -XPOST localhost:8088/v1/filter/delete \
  -d "{\"key\":\"user-42\",\"credential\":\"$CRED\"}"
```
完整脚本：`scripts/demo.sh`（含生成密钥、起停服务、攻击与重放演示）。

---

## 4. 重复键与删除语义

- 对同一键插入 3 次 → 候选桶中有 3 份相同指纹副本，得到 3 张**不同**凭证；
  `lookup.observed_copies = 3`，`member = true`。
- 每张凭证精确对应其中**一个槽位**。删除时：校验 HMAC（凭证绑定键）→ 在归属表中
  定位该凭证的唯一槽 → 仅清零该槽 → 移除账本项 → 原子落盘。
- 删除一张凭证后 `observed_copies = 2`；同一凭证再次使用返回 `CREDENTIAL_EXHAUSTED`。
- 三份副本都凭各自凭证删除后 `member = false, observed_copies = 0`。

这保证：**指纹假阳性只会影响 `lookup`，无法被用来执行删除**——没有对应键的有效、
未消费凭证，删除一律被拒，因此不会误伤共享同一指纹的其他键（避免假阴性来源）。

---

## 5. 独立验证（不是"接口能调用"式测试）

参考答案**不依赖被测核心自身**：验证器内置一份按规范独立重写的 XXH64
（`crates/cuckoo-verify/src/xxh64.rs`），其正确性由「空串 KAT + 与第二份第三方实现
xxhash-rust 在 5 个 seed × 长度 0–300 上的全量交叉比对」锚定；再用它独立推导指纹、
候选桶，并独立抄写一份迁移参考模型。

运行：

```bash
# 工作区全部单元/集成测试
cargo test

# 9 个独立验证场景（文本日志 + 可选 JSON 报告，退出码非 0 表示有失败）
cargo run -p cuckoo-verify -- --run-id local-001 --json verify-reports
```

场景（断言**具体值/具体失败类别**，日志含运行身份、版本、进度、计算步骤、观察/期望）：

| 场景 | 断言内容 |
|---|---|
| S01 | 2000 个固定键的 `(fp,i1,i2)` 与独立参考答案逐键一致；指纹范围；alt 对合 |
| S02 | 独立迁移模型与被测内核对 120 键给出相同成败类别、落点与整条 evicted 链、逐槽布局 |
| S03 | 小桶强制迁移环；`FILTER_FULL(kicks=上限)`；失败后槽位/计数完整回滚；存活键零假阴性 |
| S04 | 重复插删副本计数精确为 3→2→0；错键/重放分别 `INVALID_CREDENTIAL`/`CREDENTIAL_EXHAUSTED` |
| S05 | 容量耗尽后每个失败都是 `FILTER_FULL`；成功键逐个查询零假阴性；无凭证删除被拒 |
| S06 | 落盘重启存活键零假阴性；跨重启重放被拒；参数错配拒绝打开；字节翻转快照被拒 |
| S07 | 固定种子测 FPR（f=8 实测约 1.5%，f=4 对照显著更高），成员零假阴性 |
| S08 | 伪造/篡改/越键/异主密钥/重放五类攻击分类拒绝，且攻击零副作用 |
| S09 | 快照槽位-归属不一致、重复 jti、指纹超界、occupied 造假等全部显式拒绝 |

最近一次本地运行结果：**9/9 场景、55/55 步骤全部通过**；全工作区
`cargo test` 全部通过（core 15、persist 7、api 单元 3 + HTTP 集成 8、verify 2）。

---

## 6. 持久化与安全要点

- 快照格式见 `docs/format.md`：magic、格式版本、参数、槽位段、与槽一一对应的
  16 字节归属 jti 段、XXH64 校验和。装载时校验魔数/版本/参数/指纹范围/槽与归属
  对齐/jti 唯一/占用计数/校验和，任何不符都报错，绝不半开。
- 主密钥仅用于 HMAC，不出现在日志里（日志只记录来源 `env:`/`file:`）。
- 每次变更后自动原子落盘；**落盘失败会回滚内存状态**，避免内存/磁盘分裂。

## 7. 剩余限制（如实说明）

1. **单实例、全量快照、每次变更 fsync**：适合中小容量与低/中吞吐；不是高并发或
   增量 WAL 设计。写操作由单把 `Mutex` 串行化。
2. **FPR 是经典 Cuckoo Filter 的量级**（b=4 时约 `~2/2^f`，随负载上升）；
   本实现不做半排序桶（semi-sorted）等压缩。
3. **归属表使删除精确但占空间**：每个存活副本额外 16 字节 jti（4096 槽约 +64 KiB）。
4. HMAC 防伪造但**不加密键内容**；服务本身不做 TLS/调用方鉴权/速率限制，
   需置于受控网络或反向代理之后。
5. 删除凭证**不可离线吊销**：一次性以"删除即消费"实现；需要保留键而仅作废凭证的
   能力不在当前范围内。
6. 参数（桶数/桶大小/指纹位宽/迁移上限）在建库后固定，改参数必须新建过滤器。
