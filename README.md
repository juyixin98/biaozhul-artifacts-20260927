# 本地纠删码服务（Reed–Solomon / GF(2⁸) / Axum）

一套从空工程搭建的 **k 数据片 + m 校验片** 纠删码后端：编码、缺片重建、
完整性检查。Rust + Axum + 本地文件系统，所有依赖均可离线声明、可本地启动，
输入全部为合成数据。

- 有限域、矩阵、分片布局**固定且可审查**（无随机参数、无平台相关行为）。
- 严格区分**缺失片**（missing）与**校验失败的坏片**（bad_digest），坏片按擦除处理。
- 可用好片不足 k 时**明确拒绝，绝不返回任何推算/伪造数据**。
- 原始长度与填充字节数在清单（manifest）的**受校验字段集**之内。
- 独立参考答案由 **Python 独立实现**（`tests/reference/oracle.py`）生成，
  不调用任何被测 Rust 代码；Rust 测试逐字节对照。

---

## 1. 算法假设（固定，非可配置）

| 项 | 取值 | 说明 |
|---|---|---|
| 有限域 | GF(2⁸)，模多项式 `0x11B` = x⁸+x⁴+x³+x+1 | AES/Rijndael 多项式，在 GF(2) 上不可约 |
| 对数/指数表生成元 | `g = 3`（元素 x+1） | **注意：在 0x11B 域中 2 不是本原元，3 才是**；表构建对此有断言 |
| 乘法 | 加法=XOR；乘法用 exp/log 表 O(1) | 另保留表无关的教科书式 `mul_slow`，对全部 65 536 对输入交叉校验 |
| 编码矩阵 | 系统码 `A = [ I_k ; C ]`，`C` 为 Cauchy 矩阵 | `C[p][j] = 1/(x_p XOR y_j)`，`y_j = j`，`x_p = k+p` |
| MDS 性质 | 任取 k 个不同片的行组成的方阵必可逆 | Cauchy 行列式非零；对小配置**穷举所有 C(n,k) 组合实证** |
| 恢复 | 对所选 k 行 `A_sel·D = S` 做 GF(2⁸) 高斯–约当消元 | 零主元即奇异，返回错误，绝不出部分/猜测结果 |
| 分片布局 | 原文零填充到 `k·shard_len`，连续切 k 份 | `shard_len = ceil(len/k)`，空对象取 1 |
| 容错 | 任意 ≤ m 个片缺失**或**损坏可恢复 | `k+m ≤ 255`（域内需要 k+m 个互异点） |
| 片摘要 | `SHA-256("ec-shard-v1" ‖ u16be(片号) ‖ u64be(长度) ‖ 字节)` | 含**片号**：交换两个有效片也能检出；比较为常数时间 |
| 清单摘要 | 对确定性 TLV「受覆盖编码」求 SHA-256 | 覆盖版本、域、k、m、shard_len、**original_len、pad_len**、算法、对象 id、全部片摘要 |

**为什么坏片可以安全当作擦除**：恢复只使用「位置相关摘要」验证通过的片；
任何被翻转/截断/换位的片都不会进入方程。剩余任意 k 个好片对系统码 Cauchy
矩阵都足以唯一确定原文。

**原长度与填充为何不可伪造**：`original_len` 与 `pad_len` 都在清单摘要覆盖
范围内。攻击者即使保持 `k·shard_len - original_len = pad_len` 的算术一致，
重算的清单摘要也不匹配，返回 `MANIFEST_DIGEST_MISMATCH`（测试已固化）。

---

## 2. 模块关系（多模块后端，无硬编码演示）

```
crates/
├── ec-gf/       有限域原语：exp/log 表、教科书乘法、逆元、KAT
├── ec-core/     编码/索引内核：config、Cauchy 矩阵、高斯消元、
│                编码/重建/恢复、片摘要、类型化错误
├── ec-format/   数据格式：版本化 manifest、确定性 TLV 受覆盖编码、JSON 落盘形式
├── ec-store/    持久化适配：ObjectStore 端口 + 文件系统原子写实现 + 内存实现
├── ec-api/      验证/服务接口：编排服务、分类报告、Axum 路由、请求关联
└── ec-server/   启动二进制：加载 JSON 配置、打开存储、优雅退出

config/          服务 JSON 配置（无 toml 依赖）
scripts/         run_tests.sh（全量验证）、e2e.sh（真实 HTTP 故障注入）
tests/reference/ oracle.py（独立 Python 参考答案）+ fixtures.json（黄金向量）
```

依赖方向严格向下：`ec-server → ec-api → {ec-store → ec-format → ec-core → ec-gf}`。
内核与格式层**无任何 I/O**，可确定性测试。

### HTTP 接口

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `/health` | 健康检查，回显域与编码信息 |
| POST | `/v1/objects` | 编码并持久化（body：`object_id?`, `data_b64`） |
| GET | `/v1/objects/{id}/verify` | 完整性检查：逐片分类、可恢复性、关键步骤 |
| POST | `/v1/objects/{id}/decode` | 恢复原文（`?include_data=false` 只判可行性） |
| POST | `/v1/objects/{id}/repair` | 重建缺/坏片并原子落盘（`targets:[]` = 全部） |
| POST | `/v1/decode` | **无状态**恢复：调用方自带 `manifest_json` + 片 |

所有响应/错误都带 `x-request-id`（可由请求头指定，否则生成 UUID），
错误有稳定机器码：`INSUFFICIENT_SHARDS`、`DUPLICATE_SHARD_INDEX`、
`DIGEST_MISMATCH`、`MANIFEST_DIGEST_MISMATCH`、`SIZE_MISMATCH` 等。
报告里 `failures`（硬失败）与 `uncertainties`（不确定结论，如读错误）**分列**。

### 磁盘布局

```
<storage_dir>/<object_id>/manifest.json
<storage_dir>/<object_id>/shards/shard-00000.bin … shard-00004.bin
```
提交顺序：先原子写全部片（临时文件→fsync→rename），最后写清单；
`put_object` 后回读校验清单摘要，失败不报告成功。

---

## 3. 依赖版本（均来自本地 cargo 缓存，离线可构建）

Rust 工具链：`rustc 1.98.1` / `cargo 1.98.1`，edition 2021。

| crate | 版本 | 用途 |
|---|---|---|
| axum | 0.8.9 | HTTP 路由/提取器 |
| tokio | 1.53.1 | 异步运行时（阻塞服务走 spawn_blocking） |
| tower | 0.5.3 | 请求 id 中间件 |
| serde / serde_json | 1.0.229 / 1.0.151 | JSON DTO 与清单 |
| sha2 | 0.10.9 | SHA-256 |
| uuid | 1.26.1（v4） | 对象/请求 id |
| base64 | 0.22.1 | 二进制线格式 |
| tracing / tracing-subscriber | 0.1.44 / 0.3.23 | 结构化日志 |
| anyhow | 1.0.104 | 仅启动二进制 |

Python 参考答案仅用标准库（hashlib/json/struct），Python 3.12 验证。
未使用任何需要联网的过程宏或外部服务；`cargo build --offline` 与
`cargo build --offline --release` 均已实测通过。

---

## 4. 本地验证命令与预期判断

### 4.1 一键全量（推荐）
```bash
./scripts/run_tests.sh
# 预期结尾： passed total 55 / failed total 0 / RESULT : ALL GREEN
```
脚本会先用独立 Python 实现**重新生成**黄金向量，再离线构建并运行全部测试。

### 4.2 端到端（真实起服务 + curl 故障注入）
```bash
./scripts/e2e.sh            # 默认端口 8080；PORT=8095 ./scripts/e2e.sh
# 预期结尾： ALL END-TO-END CHECKS PASSED
```
覆盖：健康/请求关联 → 编码 → 逐字节解码 → 缺片 → 坏片 →
超容错 409 且响应无任何数据 → 修复落盘 → **Python 独立实现读取 Rust
磁盘对象恢复出完全相同字节**。

### 4.3 手动
```bash
cargo test --offline --workspace      # 全部单元 + 集成测试
cargo build --offline --release
./target/release/ec-server --config config/service.example.json
```

### 如何判断结果（不是只看「接口能调」）
- **黄金向量**：Rust 编码出的每个片、每条文摘、TLV 字节都与 Python 答案
  `fixtures.json` 逐字节相等。
- **全组合**：对 (k=3,m=2) 与 (k=4,m=2)，枚举全部 C(n,0..m)=**54** 个可恢复
  擦除组合与全部 C(n,k)=**35** 个恰好 k 片幸存组合，恢复结果逐字节等于原文。
- **失败类别**：重复片号→`DUPLICATE_SHARD_INDEX`；清单不符→
  `MANIFEST_DIGEST_MISMATCH`；m+1 片丢失→`INSUFFICIENT_SHARDS`（HTTP 409，
  响应中断言不存在 `data_b64`）；单片损坏→摘要判坏 + 按擦除恢复成功。

---

## 5. 测试清单与状态（如实标注）

| 测试 | 断言内容 | 状态 |
|---|---|---|
| `ec-gf` 单元 ×4 | 表遍历整群、65 536 对双乘法一致、域公理、AES KAT(0xC1/0xCA/0x1B) | ✅ 通过 |
| `ec-core` 单元 ×10 | 配置边界、Cauchy KAT、全组合 MDS 可逆、填充往返、各类拒绝 | ✅ 通过 |
| `ec-format` 单元 ×3 | 清单往返、逐字段篡改破摘要、填充算术 | ✅ 通过 |
| `ec-core/tests/golden_vectors` ×4 | 编码/摘要/域 KAT 对照 Python | ✅ 通过 |
| `ec-core/tests/exhaustive_recovery` ×6 | 54 组合、35 幸存集、超容错精确类别、重建逐片对照、重复片号、单片损坏 | ✅ 通过 |
| `ec-format/tests/manifest_format` ×7 | TLV 逐字节对照、original_len/pad_len 两层防护、跨层恢复 | ✅ 通过 |
| `ec-store/tests/fs_store` ×5 | 真实磁盘布局、缺失≠错误、清单篡改、修复写、越权 id | ✅ 通过 |
| `ec-api` 单元 ×3 | 配置加载/默认值、非法编码参数、坏 JSON | ✅ 通过 |
| `ec-api/tests/http_api` ×13 | 真实 Axum：往返、分类、全组合、超容错无数据、重复片号、无状态、清单篡改、修复、请求 id | ✅ 通过 |
| `scripts/e2e.sh` | 真实进程全链路 + Python↔Rust 跨实现恢复 | ✅ 通过 |

合计 **55 个 cargo 测试 + 1 个端到端脚本**，全部在本环境实际运行通过；
无「未运行却声称通过」的测试。`clippy` 组件不在本地缓存中，未运行（已如实
标记）；以 `cargo build` **零警告**作为替代静态检查。

---

## 6. 可解释性

- 每个请求有 `request_id`，日志与 JSON 报告使用同一词汇（good/missing/
  bad_digest/rebuilt）。
- 报告含 `steps`：分拆填充、施加 Cauchy 行、摘要、求解所用片号、按认证长度
  截断、修复落盘等关键步骤与格式版本 `format_version=1`、域标识、存储位置。
- 启动日志打印全部假设（域、矩阵、完整性方案、存储目录、版本）。
