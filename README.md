# Wavelet Matrix Service（整数序列索引后端）

对 `i64` 整数序列构建 **Wavelet Matrix（小波矩阵）** 索引，提供以下区间查询：

| 能力 | 说明 |
|---|---|
| 区间第 k 小 `kth_smallest(l, r, k)` | 0-based k；`k=0` 即区间最小值 |
| 值域计数 `count_lt(l, r, bound)` | 统计 `[l, r)` 中严格小于 `bound` 的元素数 |
| 区间值域计数 `count_range(l, r, lo, hi)` | 统计 `[l, r)` 中满足 `lo <= v < hi` 的元素数 |
| 前驱 `predecessor(l, r, bound)` | 区间内严格小于 `bound` 的最大值，可为空 |
| 后继 `successor(l, r, bound)` | 区间内大于等于 `bound` 的最小值，可为空 |

所有查询由 Wavelet Matrix 索引逐层游走（rank + 稳定分区映射）完成，
**不存在任何“切片后排序”的实现路径**（见 `src/wavelet_matrix.rs`）。

## 语义约定（已在代码与文档中统一）

- **区间半开 `[l, r)`**，端点 0-based：要求 `0 <= l < r <= len`。
  - `l == r` → `empty_range` 错误（空区间明确拒绝）；
  - `l > r` 或 `r > len` → `invalid_range` 错误。
- **k 为 0-based**：合法范围 `0 <= k < r - l`，越界 → `k_out_of_bounds`。
- **坐标压缩保序**：对排序去重后的原值分配递增 rank（`src/compress.rs`），
  rank 的大小序与原值完全一致；因此重复值天然正确，
  `i64::MIN / i64::MAX` 不需要任何特例处理。
- 索引在 rank 上构建，矩阵高度 = 容纳不同值数所需的最小位数（至少 1）。

## 模块结构（多模块后端，各模块承担实际职责）

```
src/
  bitvector.rs       # rank 支持位向量（索引原语，O(1) rank1/rank0）
  wavelet_matrix.rs  # 索引内核：u64 rank 上的小波矩阵
                     #   - quantile：逐位游走求第 k 小
                     #   - range_freq：逐位计数 < upper
  compress.rs        # 保序坐标压缩（sorted+dedup，binary_search 映射）
  index.rs           # i64 索引门面：五类查询 + 全部参数校验/错误分类
  format.rs          # 二进制数据格式 WMX1：magic/版本/长度/载荷/FNV-1a 校验
  store.rs           # 文件系统持久化适配：原子写、名称校验、列举/加载
  config.rs          # 配置解析（独立测试）
  api.rs             # Axum 验证接口（请求身份、结构化错误、步骤日志）
  main.rs            # 服务入口：加载配置 -> 恢复磁盘索引 -> 启动 HTTP
tests/
  wm_reference.rs    # 随机差分测试：对照“排序切片 + 线性计数”朴素 oracle
  persistence.rs     # 构建/保存/加载一致性、损坏/截断/版本错误分类
  api_tests.rs       # HTTP 层断言（手算答案 + 状态码 + error.kind）
fixtures/
  sample_arrays.json # 最小合成夹具（含手算答案）
config/default.toml  # 默认配置
examples/query_examples.sh
docs/
  REPRODUCING.md     # 复现文档
  run_logs/          # 真实运行保留结果（测试/HTTP/服务日志）
```

## 快速开始

```bash
cargo build --release

# 测试（全部为确定性测试，无需网络/账号）
cargo test

# 启动服务（默认 127.0.0.1:18080，数据目录 ./data）
./target/release/wavelet-matrix-service --config config/default.toml
# 另一终端：
bash examples/query_examples.sh
```

### 调用示例

```bash
# 建索引
curl -s -X POST http://127.0.0.1:18080/v1/indexes \
  -H 'X-Request-Id: demo-1' -H 'Content-Type: application/json' \
  -d '{"name":"demo","values":[5,-3,7,7,0,-3,42,1]}'

# 第 3 小（0-based；排序后为 [-3,-3,0,1,5,7,7,42]，答案 1）
curl -s -X POST http://127.0.0.1:18080/v1/indexes/demo/queries \
  -H 'Content-Type: application/json' \
  -d '{"op":"kth_smallest","l":0,"r":8,"k":3}'

# 计数值 < 8 的元素（答案 7）
curl -s -X POST http://127.0.0.1:18080/v1/indexes/demo/queries \
  -H 'Content-Type: application/json' \
  -d '{"op":"count_lt","l":0,"r":8,"bound":8}'
```

每个响应都带请求身份：

```json
{"request_id":"demo-1","ok":true,"result":{ ... }}
```

失败时失败原因单列在 `error` 中，`kind` 稳定可机器判定：

```json
{"request_id":"...","ok":false,
 "error":{"kind":"k_out_of_bounds",
          "message":"k = 8 is out of bounds for a window of length 8 (k is 0-based)"}}
```

## 错误类别（HTTP 状态码）

| kind | 状态码 | 触发条件 |
|---|---|---|
| `empty_input` | 400 | 对空数组建索引 |
| `invalid_range` | 400 | `l > r` 或 `r > len` |
| `empty_range` | 400 | `l == r` 空区间 |
| `k_out_of_bounds` | 400 | `k >= r-l` |
| `invalid_index_name` | 400 | 名称非法（防路径穿越） |
| `bad_request` | 400 | JSON 非法、缺参数、未知 op |
| `index_not_found` | 404 | 索引不存在 |
| `duplicate_index` | 409 | 同名索引已存在 |
| `corrupt_format` | 500 | 磁盘文件 magic/结构/校验和错误 |
| `unsupported_version` | 500 | 格式版本不兼容 |

## 持久化格式（`WMX1`，版本 1）

```
magic b"WMX1" | version u32 | payload_len u64 | payload | checksum u64
payload = len u64 | distinct u64 | height u32
        | distinct × i64（升序原值表）
        | height × { zeros u64 | bits_len u64 | ceil(bits_len/64) × u64 位字 }
```

- 位向量的 rank 前缀表不入库，加载时由位字重建，杜绝派生状态不一致；
- 载荷末尾 64 位 FNV-1a 校验和；写盘走临时文件 + rename 原子替换；
- 加载校验：magic、版本、长度、尾部冗余位、层级数、值域表严格递增等；
- 服务启动时自动从 `data_dir` 恢复全部索引（损坏文件会中止启动）。

## 诊断与可解释性

- 每个请求可由 `X-Request-Id` 头指定身份；缺省时生成 `req-<uuid>`，
  响应体原样回显，日志逐条关联。
- 建索引日志分阶段记录：`request received`（输入规模）→ `kernel built`
  （len/distinct/height）→ `persisted`（路径、字节数、`format_version`）。
- 查询日志记录 index/op/l/r 与结果；失败单独 WARN，带 `error_kind` 与状态码。
- “不确定结论”在响应中以显式空结果表达：
  `predecessor/successor` 未命中时返回 `"found": false, "value": null`，
  而不是编造一个值。
- 保留的真实运行记录见 [`docs/run_logs/`](docs/run_logs/)，
  复现步骤见 [`docs/REPRODUCING.md`](docs/REPRODUCING.md)。

## 测试策略要点

- **参考答案独立**：差分测试的 oracle 是朴素的“复制窗口→排序”和线性扫描
  （`tests/wm_reference.rs`），与被测内核没有共享实现；HTTP 测试使用
  夹具上手工计算的答案（`fixtures/sample_arrays.json` 同步留存）。
- 覆盖：随机小数组 × 多个值域、全同值（含 `i64::MIN/MAX`）、
  负值、有符号极值与位宽边界、单元素；对每个数组穷举所有 `[l,r)`
  窗口与所有合法 k，边界值含 `v-1/v/v+1` 与 `±∞`。
- 失败路径断言**具体错误枚举/kind 与状态码**，不是只断言“调用失败”。

依赖版本由 `Cargo.lock` 锁定。工具链：Rust 1.98.1（见 `docs/run_logs/toolchain.txt`）。
