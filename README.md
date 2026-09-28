# lz77-blocks — 带滑动字典的 LZ77 块压缩后端

Rust + Axum + 文件系统实现。块（block）有两种依赖模式：

* **独立块（independent）**：空字典，自包含，可单独解码；
* **依赖块（dependent）**：绑定前置块输出尾部窗口的 SHA-256 摘要，必须按链解码。

所有输入均为本地合成夹具，不依赖任何生产账号或外部服务；唯一的外部进程是仓库内
自带的独立 Python 参考实现（标准库），用于交叉验证。

## 固定参数

| 常量 | 值 | 位置 |
|---|---|---|
| 回溯窗口 `WINDOW_BYTES` | 4096 | `src/format.rs` |
| 最短匹配 `MIN_MATCH` | 3 | `src/format.rs` |
| 最长匹配 `MAX_MATCH` | 258（长度域 1 字节：3+255） | `src/format.rs` |
| 单块输出绝对上限 | 8 MiB | `MAX_OUTPUT_BYTES` |
| 声明解压倍率上限 | 100× | `MAX_EXPANSION_RATIO` |
| 依赖链深度上限 | 64 块 | `MAX_CHAIN_BLOCKS` |

## 模块边界（数据与错误契约）

```
src/
  error.rs    错误分类法：CodecError + ErrorCategory
  format.rs   线格式 / 信封 / CRC32 / 字典摘要 / 安全上限（纯数据层）
  lz77.rs     编码索引内核（哈希链）+ 逐字节重叠复制解码器
  codec.rs    块级绑定（独立/依赖摘要校验）、链解码、无状态验证
  store.rs    文件系统持久化适配（manifest.json + blocks/*.lz71，原子提交）
  service.rs  Axum 路由、DTO、错误分类 -> HTTP 状态映射
  main.rs     服务二进制
```

错误分类（跨模块稳定字符串，也是 HTTP 错误体与测试日志的字段）：

| `ErrorCategory` | 字符串 | HTTP | 含义 |
|---|---|---|---|
| `Input` | `input_error` | 400 | 线数据畸形、参数非法、CRC 错、越窗距离 |
| `StateConflict` | `state_conflict` | 409 | 缺前块、摘要不匹配、链断裂、乐观锁过期 |
| `ResourceExhausted` | `resource_exhausted` | 413 | 声明输出超绝对上限/倍率/链深 |
| `NotFound` | `not_found` | 404 | 块 id 不存在 |
| `ComputeFailure` | `compute_failure` | 500 | 内部不变量/主机 I/O 故障 |

四类“输入错误 / 状态冲突 / 资源耗尽 / 计算失败”因此可被程序化区分。

## 关键正确性要点

1. **重叠复制逐字节**：匹配长度允许大于回溯距离（RLE 式自重叠）。解码器用
   `work[src + k]` 逐字节读取，后一次迭代能读到本次刚写入的字节；绝不使用会读取
   “尚不存在切片”的整块 `copy_from_slice`。见 `src/lz77.rs` 解码器及
   `self_overlap_*` 测试。
2. **跨窗口边界**：距离恰为 4096 合法、4097 被拒（u16 能编码 4097，但固定窗口不允许）。
3. **依赖块绑定前置摘要**：帧头携带编码器所见字典尾窗的 SHA-256；接收方字典不符
   即 `state_conflict`，而不是把好端端的帧误判成 `input_error`。
4. **解压不按恶意长度分配**：解码前只校验信头部字段（绝对 8 MiB + 100×倍率），
   解码器 `Vec` 不按 `data_len` 预分配；200 次 1 GiB 声明帧解码的 RSS 增长 < 16 MiB。
5. **原子持久化**：先 `write tmp + rename` 帧文件，再重写 manifest，崩溃后不会出现
   manifest 指向半成品帧的情况。

## 快速开始

```bash
cargo build --release
./target/release/lz77-blocks --store ./data/store --addr 127.0.0.1:8080
```

### 服务调用示例（会自行构建并启动一个临时服务，跑完即停）

```bash
./scripts/service-calls.sh
```

脚本覆盖全部路由的正常调用，以及 400 / 404 / 409 / 413 四类异常。手动示例：

```bash
# 独立块
curl -s -X POST localhost:8080/blocks -H 'content-type: application/json' \
  -d "{\"mode\":\"independent\",\"data\":\"$(printf 'abcabcabc' | base64)\"}"

# 依赖块（prev_id 乐观锁）
curl -s -X POST localhost:8080/blocks -H 'content-type: application/json' \
  -d '{"mode":"dependent","prev_id":"blk-00000000","data":"...base64..."}'

# 列表 / 单块解压 / 整链解压 / 原始帧
curl -s localhost:8080/blocks
curl -s localhost:8080/blocks/blk-00000001/raw
curl -s localhost:8080/chain/raw
curl -s localhost:8080/blocks/blk-00000001/frame -o child.lz71

# 不落盘验证一个帧
curl -s -X POST localhost:8080/validate -H 'content-type: application/json' \
  -d '{"frame":"...base64...","mode":"independent"}'
```

请求/响应中的字节字段一律标准 base64。错误体：
`{"error":{"category":"state_conflict","detail":"..."}}`。

## 复现测试（证据）

```bash
./scripts/run-tests.sh
```

该脚本：

1. 用独立 Python 参考实现重新生成确定性夹具（`tests/fixtures/`）；
2. 分配单调**运行编号**（`run-YYYYmmddTHHMMSSZ-NNN`），导出环境变量让四个测试
   二进制归入同一目录；
3. `cargo test`（15 个单元测试 + 4 个集成套件）；
4. 聚合每个用例的判定与中间状态到 `tests/test-logs/<run>/summary.json`。

最近一次真实运行的存档在 `reports/`：

* `test-run.txt` — 完整 cargo 输出；
* `test-summary.json` — 78 个集成判定（41 交叉验证 / 10 资源限制 / 17 接口 / 10 持久化）；
* `service-calls.txt` — 真实 HTTP 服务 14 个正常/异常调用的留痕；
* `reference-decode-report.json` — Python 参考解压器对自重叠夹具的独立判定。

结果（见 `reports/`）：**33 个 cargo 测试 + 78 条带中间状态的集成判定全部通过**。

### 测试如何保证不是“被测核心自证”

`reference/ref_lz77.py` 是只依据线格式规范独立编写的第二实现：

* 编码器用 **O(n·W) 暴力最长匹配**（Rust 侧是 8192 槽哈希链，算法不同）；
* 完整性用 **zlib.crc32 / hashlib.sha256**（Rust 侧 CRC 是本地表驱动实现）；
* 独立的逐字节 token 解码器。

`tests/oracle_crosscheck.rs` 做双向交叉：

* Python 生成的夹具帧，Rust 与 Python **都解码**，逐字节 + SHA-256 比对，并要求
  异常夹具在两侧给出**相同错误类别**；
* Rust 编码器产的帧交给 Python 解码、Python 编码器产的帧交给 Rust 解码；
* 四种分块模式（单独立块 / 三个独立块拼接 / 3 块依赖链 / 5 块另切依赖链）恢复出的
  字节与 `source.bin` 完全一致。

断言是具体值与具体失败类别，而非“接口可调用”。

## 夹具

`tests/fixtures/`（由 `reference/ref_lz77.py gen-fixtures` 确定性生成，已签入）：

* `source.bin` — 20 KB 合成语料（固定短语重复 + 固定种子 LCG 伪随机 + 工程化的
  距离恰为 4096 的跨窗匹配尾部）；
* `good/` — 单独立块、三等分独立块、3 块/5 块依赖链、自重叠 RLE、跨窗边界；
* `malformed/` — 截断头、坏魔数、坏 CRC、距离指向流起点之前、距离 4097、
  缺前块、错误字典、1 GiB 声明炸弹、倍率炸弹、超产、尾部余字节、未知控制字节；
* `fixtures_manifest.json` — 每个夹具的期望结论、失败类别、关键中间状态与判定理由。

## 依赖与可复现构建

`Cargo.lock` 已签入；`cargo fetch --offline` 验证过锁定依赖可离线解析。
工具链：Rust 1.98（edition 2021）。Python3 仅测试参考实现需要（标准库，无第三方包）。

```bash
cargo test                 # 直接跑
cargo clippy --all-targets # 零警告
cargo build --release
```

## 线格式

见 [`docs/FORMAT.md`](docs/FORMAT.md)。
