# ec-service — 本地 Reed-Solomon（k 数据片 + m 校验片）纠删码服务

一套从空工程搭建的、多模块的纠删码后端：把任意字节对象编码成 `k` 个数据片
和 `m` 个校验片持久化到文件系统，支持**缺片/坏片重建**与**完整性检查**。
核心有限域运算、矩阵构造与恢复过程均可在 `src/gf256.rs`、`src/erasure.rs`
中逐行审查；服务严格区分「缺失片」与「校验失败的坏片」，坏片按擦除处理；
**可用（通过校验的）片少于 k 时绝不返回任何数据**；原始长度与填充纳入受
校验清单。

- 语言/运行时：Rust（edition 2021，本机 `rustc 1.98.1`）
- Web 框架：Axum 0.8（Tokio）
- 存储：本地文件系统（无外部服务）
- 哈希：SHA-256（`sha2`）

---

## 1. 算法假设（固定、可审查）

| 项 | 取值 | 位置 |
|---|---|---|
| 有限域 | GF(2⁸)，元素为一个字节 | `src/gf256.rs` |
| 本原多项式 | `x⁸+x⁴+x³+x²+1` = `0x11d`（285） | `gf256::PRIMITIVE_POLY` |
| 乘法生成元 | `g = 2`（乘法群阶 255） | `gf256::GENERATOR` |
| 加/减法 | XOR | `gf256::add` |
| 乘法 | log/antilog 表：`a·b = exp(log a + log b)`，表在启动时由本原多项式**确定性生成**（非硬编码魔数） | `gf256::mul` |
| 版本标记 | `GF256-PP0x11D-G2/v1`，写入每个清单 | `gf256::GF_VERSION` |

**编码矩阵（系统化 Vandermonde）**

- 取 Vandermonde 矩阵 `V[i][j] = α_i^j`，其中 `α_i = i+1`
  （域元素 `1..=k+m`，互不相同且非零），形状 `(k+m) × k`。
- 任意选取 Vandermonde 矩阵的 k 行（评估点互异非零）都可逆：其行列式为
  非零系数乘以所有 `(α_i − α_j)` 的乘积，在 GF(2⁸) 中不为 0。
- 令前 k 行为 `A`，后 m 行为 `P`。系统化校验矩阵 `C = P·A⁻¹`，则编码矩阵为
  `E = [ I ; C ]`（前 k 行是单位阵，故数据片就是原始字节切片）。
- **分片布局固定**：片号 `0..k` 为数据片，`k..k+m` 为校验片；输入零填充到
  `k · shard_len`，**原始长度** `original_len` 与 `pad_len` 存在清单中并参与
  摘要，绝不从填充后的片反推长度。

**恢复（擦除译码）**

1. 仅使用 SHA-256 与清单一致的片；缺失片和坏片都视为「擦除位置」，坏片
   字节**绝不**进入解码器。
2. 任取 k 个可用片，取其编码矩阵行组成 `A_sel`，Gauss–Jordan 求逆。
3. 逐字节列求解数据片 `D = A_sel⁻¹·Y`，再用 `[I; C]` 重建全部 k+m 片。
4. 拼接数据片、按清单里认证过的 `original_len` 截断填充，并对结果再算一次
   SHA-256，与清单 `payload_sha256` 比对一致后才返回。
5. 可用片 `< k`：返回 `NOT_RECOVERABLE`（HTTP 409），**不返回任何载荷字节**。

容错能力：可容忍任意 ≤ m 个片（数据片/校验片、缺失/损坏任意混合）不可用。

---

## 2. 模块关系（各自承担实际工作，非硬编码演示）

```
HTTP (Axum)                src/api.rs       路由、请求ID中间件、错误->HTTP 映射
   │
服务编排                    src/service.rs   编码/取回/检查/修复；强制"不足k片不伪造"
   │
   ├── 编码/索引内核        src/erasure.rs   系统化Vandermonde编码 + 擦除重建
   │      └── GF(2⁸)运算    src/gf256.rs     域运算、矩阵求逆（可审查核心）
   │
   ├── 持久化适配           src/storage.rs   文件系统原子写、逐片审计(缺失/坏片)
   │
   └── 数据格式             src/manifest.rs  JSON清单 + 规范化SHA-256(认证长度/填充/片摘要)

配置                        src/config.rs    仅环境变量，本地默认值
统一错误                    src/error.rs     稳定错误码 + HTTP状态 + 可解释消息
入口                        src/main.rs      初始化日志/配置/服务
```

**磁盘布局**

```
<EC_DATA_DIR>/<object_id>/
    manifest.json        # 经 manifest_digest 认证
    shard-000.bin ...    # k+m 个定长片
```

写入是原子的：先写临时目录 / `*.tmp`，成功后 `rename` 到位；修复时新旧目录
交换并带回滚。对象 id 仅允许 `[A-Za-z0-9._-]`，拒绝路径穿越。

**清单认证**：`manifest_digest = SHA256(规范化JSON(除digest外所有字段))`，
规范化形式为解析成 JSON 值后按 key 排序、无空白序列化（跨语言可复现）。
被覆盖的内容包括：`k,m, original_len, shard_len, pad_len, payload_sha256,
algorithm(scheme/field/format_version), shards[每片的index/role/file/size/sha256]`。
篡改原始长度、任一片摘要、片清单长度或算法版本，都会导致 `MANIFEST_DIGEST_MISMATCH`
（即使重新计算摘要自洽地伪造，也会被 `UNSUPPORTED_VERSION` 等结构校验拦住）。

---

## 3. HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/healthz` | 健康检查，返回域与格式版本 |
| GET | `/v1/config` | 允许的 (k,m) 配置、大小上限、算法版本 |
| GET | `/v1/objects` | 列出对象 |
| PUT | `/v1/objects/{id}?k=&m=` | 编码并存储（body 为原始字节），201 |
| GET | `/v1/objects/{id}` | 校验→重建→截断→返回原字节；不足 k 片返回 409 |
| GET | `/v1/objects/{id}/inspect` | 完整性检查（不改动数据），逐片分类 |
| POST | `/v1/objects/{id}/repair` | 重建缺失/坏片并落盘，再从盘复审 |

每个响应都带 `x-request-id`（也接受请求方传入同名字段做关联）；服务端日志
在 `request` span 内带 `request_id`，记录编码、审计、重建等关键步骤；失败原因
与「不可恢复」这类不确定结论单独以 warn 记录。错误体统一为：

```json
{ "ok": false,
  "error": { "code": "NOT_RECOVERABLE",
             "message": "...", "detail": { "need": 3, "missing": [], "corrupt": [] } } }
```

---

## 4. 依赖版本（`cargo generate-lockfile` 后锁定，见 Cargo.lock）

| crate | 版本 | 用途 |
|---|---|---|
| axum | 0.8.9 | HTTP 框架 |
| tokio | 1.53.1 | 异步运行时 |
| serde / serde_json | 1.0.229 / 1.0.151 | 清单序列化 |
| sha2 | 0.10.9 | SHA-256（域无关，仅用于完整性认证） |
| hex | 0.4.3 | 摘要十六进制编码 |
| tracing / tracing-subscriber | 0.1.44 / 0.3.23 | 结构化日志 |
| uuid | 1.26.1 | 请求 ID |
| Rust | 1.98.1（stable，本机） | edition 2021 |

纠删码与有限域运算**不依赖任何第三方库**，全部在 `gf256.rs`/`erasure.rs` 内
实现。参考/测试侧仅使用 Python 3 标准库。

---

## 5. 本地验证命令与预期判断

### 5.1 重新生成独立参考向量（可选，已签入）

```bash
python3 scripts/gen_vectors.py --out tests/data/test_vectors.json
# 预期：wrote ... 6 cases, 47 recoverable erasure combinations
# 重复运行产物逐字节一致（确定性）
```

该脚本是**独立参考实现**：用俄罗斯农夫乘法（而非 Rust 的 log/exp 表）、对
增广块 `[Aᵀ | Pᵀ]` 消元（而非 Rust 的 `P·A⁻¹` 矩阵乘）推导同一套码，因此测试
答案不是由被测核心自己生成的。

### 5.2 全部自动化测试

```bash
cargo test
```

预期（当前实际结果）：

```
lib 单元测试 ............ 14 passed
external_vectors ........  5 passed   # 域事实/矩阵/分片/47组合/超容错 对外部向量
exhaustive_recovery .....  3 passed   # 4个小配置×6输入 枚举全部可恢复擦除(246模式)
failure_modes ........... 11 passed   # 坏片/重复片号/清单不符/超容错/填充 等
api_lifecycle ...........  4 passed   # 真实TCP+Axum 端到端
----------------------------------------
总计 .................... 37 passed, 0 failed
```

测试断言的是**具体结果与失败类别**，例如：
- 枚举每个小配置下所有 1..=m 片擦除组合，逐字节比对原文（不止「能调用」）；
- 重复片号 → `DUPLICATE_OR_INVALID_INDEX`；片长不符 → `SHARD_LENGTH_MISMATCH`；
- 篡改 `original_len`/片摘要 → `MANIFEST_DIGEST_MISMATCH`；
- 超过 m 个片不可用 → `NOT_RECOVERABLE`，且返回体不是载荷；
- 单片损坏 → 分类为 `corrupt`（区别于 `missing`），修复后逐片等于原字节。

### 5.3 一键端到端冒烟（真实二进制 + curl）

```bash
./scripts/verify_local.sh
```

预期最后一行：`RESULT: 33 passed, 0 failed`。脚本会自动构建 release、启动服务、
造缺片/坏片/混合擦除/超容错/清单篡改/空对象，并对 HTTP 状态码、错误类别和
「下载字节 == 原文」逐一断言。

### 5.4 手动运行

```bash
EC_DATA_DIR=./ec-data EC_BIND_ADDR=127.0.0.1:8080 cargo run --release
# 另一个终端：
curl -X PUT 'http://127.0.0.1:8080/v1/objects/demo?k=3&m=2' --data-binary 'hello'
curl -s  http://127.0.0.1:8080/v1/objects/demo/inspect | python3 -m json.tool
curl -s  http://127.0.0.1:8080/v1/objects/demo            # -> hello
```

环境变量：`EC_DATA_DIR`（默认 `./ec-data`）、`EC_BIND_ADDR`（默认
`127.0.0.1:8080`）、`EC_MAX_OBJECT_BYTES`（默认 64 MiB）、
`EC_PROFILES="2,1 3,2"`（默认 `(1,1) (2,1) (3,2) (4,2)` 这组「可枚举小配置」）、
`RUST_LOG`。

---

## 6. 测试结果如实标注

- **已运行且通过**：上文 37 个 Rust 测试、`scripts/verify_local.sh` 33 项断言，
  在本机 `rustc 1.98.1 / Linux x86_64` 实跑通过；Python 向量重复生成逐字节一致。
- **未运行/不适用**：无跨平台（Windows/macOS）、无并发压测/性能基准、无网络
  TLS 鉴权测试——本任务范围为「可声明、可启动的本地环境与合成输入」，这些未纳入。
- 已知非功能性提示：`cargo clippy` 对个别可审计数学循环报 `needless_range_loop`
  风格建议；为保持与公式对应的索引写法刻意保留，不影响正确性。
