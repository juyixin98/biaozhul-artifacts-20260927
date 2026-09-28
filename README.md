# Wavelet Matrix Service (Rust + Axum)

整数序列的 **Wavelet Matrix（小波矩阵）** 后端实现，支持：

- 区间第 k 小（range k-th smallest）
- 值域计数（value range counting，含闭区间上界变体）
- 前驱 / 后继（predecessor / successor，等值情况单独报告）
- 坐标压缩的二进制持久化与重建，以及可解释查询轨迹的 HTTP 验证接口

## 1. 语义约定（全模块统一）

| 约定 | 取值 |
| --- | --- |
| 元素类型 | `i64`（含 `i64::MIN` / `i64::MAX`） |
| 索引区间 | **半开** `[l, r)`，要求 `0 <= l <= r <= n` |
| 空区间 `l == r` | **明确拒绝**（`EMPTY_RANGE`） |
| `k` 起点 | **0 基**：`0 <= k < r-l`，越界返回 `K_OUT_OF_BOUNDS` |
| 值域区间 | 半开 `[lo, hi)`；`lo >= hi` 计数为 0 |
| 包含 `i64::MAX` 的上界 | 半开无法表达，使用内核 `range_count_inclusive`（闭区间上界） |
| 坐标压缩 | **保序**：第 j 小的不同值映射为 id `j`，重复值共享 id，有符号极值天然正确 |
| 查询算法 | 位图 block-prefix rank 上的逐层 Wavelet Matrix 导航，**不切片、不排序** |

## 2. 模块划分（多 crate 工作区，职责分离）

```
crates/
  wm-core/      编码/索引内核：BitVector(O(1) rank)、保序压缩、WaveletMatrix、错误类别
  wm-format/    数据格式：带 magic/版本/长度/FNV-1a 校验的版本化二进制容器 WMXB v1
  wm-store/     持久化适配：<data_dir>/<name>.wmi，临时文件 fsync + rename 原子落盘
  wm-server/    验证接口：Axum HTTP 服务、请求身份关联、分层配置、可解释诊断 JSON
fixtures/       最小数据夹具（混合/全同值/有符号极值）
examples/       curl 调用示例（正常 + 异常）
config/         示例 TOML 配置
tests/          见 crates/wm-server/tests：独立集成/差分/HTTP/配置测试
```

内核不依赖 serde/网络；格式层不依赖文件系统；存储层不依赖 HTTP。核心机制没有任何
硬编码演示分支。

### 持久化格式（WMXB v1，小端）

```
"WMXB" | version:u32 | payload_len:u64 | payload | fnv1a64:u64
payload = n:u64 | bit_len:u32 | distinct:u64 | values:distinct*i64
          | (zero_count:u64 | word_count:u64 | words:word_count*u64) * bit_len
```

解码顺序校验：magic → 版本 → 长度 → 校验和 → 结构一致性（位向量尾位、层级数、
每层长度、zero_count 与 popcount 一致、distinct 严格升序），任何损坏都返回具体类别。

## 3. 构建与测试

```bash
cargo build --release
cargo test                 # 24 个测试，全部为确定性/固定种子，可重复
cargo clippy --all-targets # 零警告
cargo fmt
```

测试构成（参考答案**不**来自被测内核）：

- `wm-core` 单元：手写具体期望值；**400 组随机小数组**对照独立“排序切片”
  （quantile）与“线性计数”（count/前驱/后继），覆盖全同值、负值、u8/u16/u32/
  全 64 位位宽边界。
- `wm-format` 单元：截断 / 坏 magic / 错误版本 / 长度不符 / 校验和不符 /
  结构损坏各自断言具体错误。
- `wm-store` 单元：保存-加载字节一致、重开句柄、坏名单独列出、非法名/空输入拒绝。
- `wm-server/tests/persistence_roundtrip.rs`：跨 crate 构建→保存→加载字节一致、
  夹具答案来自独立排序/线性扫描、损坏文件按类别拒绝。
- `wm-server/tests/differential.rs`：**250 组独立种子**差分压力测试 +
  诊断轨迹与答案一致性（id 可由轨迹位重建；逆序值域被显式标注）。
- `wm-server/tests/http_api.rs`：真实绑定端口的 Axum 服务黑盒测试，期望值由
  测试内独立排序生成；断言具体状态码与错误类别、请求 id 在响应头/体中关联。
- `wm-server/tests/config_layering.rs`：配置 默认 < 文件 < 环境 < CLI 分层。

## 4. 运行服务

```bash
./target/release/wm-server --config config/wm.toml
# 或用环境变量/参数覆盖：
WM_PORT=9090 ./target/release/wm-server --data-dir ./data --log-level info
```

日志为 JSON 到 stdout，每行带 `request_id`、`method`、`uri` 及查询关键字段。

### 端点

| 方法与路径 | 说明 |
| --- | --- |
| `GET /` | 服务与语义说明 |
| `GET /health` | 存活与索引数量（损坏条目单列） |
| `GET /indexes` | 列出持久化索引与元数据 |
| `POST /indexes/{name}` | `{"values":[i64...], "overwrite":bool}` 构建并落盘 |
| `GET /indexes/{name}` | 索引元数据 |
| `DELETE /indexes/{name}` | 删除索引 |
| `POST /query` | `{"op": "quantile|count|predecessor|successor", ...}` |

`{name}` 仅允许 `[A-Za-z0-9_-]`，防止路径穿越。请求体上限 16 MiB。

### 请求身份与可解释性

- 客户端可用 `X-Request-Id` 头指定 id（服务端校验为短 ASCII token），未提供时
  生成 `wm-<16 位十六进制>`；响应头与 JSON 体都回显同一 id，日志 span 也带它。
- 成功响应 `data` 为结果，`diagnostics` 含：索引名、文件位置、格式版本、位宽、
  语义说明，以及逐层导航轨迹（每层区间、rank 计数、选择的分支、值域计数的
  `count(<hi) - count(<lo)` 减法）。前驱/后继把“x 是否存在”和“是否有邻值”
  分成独立字段；没有不确定项时 `uncertain` 显式为空数组。
- 失败响应把**失败原因单列**在 `error.kind`（稳定机器码）与 `error.message`。

## 5. 调用示例与留存结果

```bash
bash examples/curl.sh            # 需要先启动服务
```

一次端到端真实运行（含正常与异常请求、落盘文件）的留存记录：

- [`examples/session.log`](examples/session.log)：对运行中的 release 服务执行
  `examples/curl.sh` 的完整 HTTP 响应（44 行），14 个步骤覆盖正常查询与全部
  异常类别，每个响应带独立 `request_id`。
- [`examples/server-json.log`](examples/server-json.log)：同次运行的服务端
  结构化 JSON 日志，成功为 INFO、失败为 WARN，字段含 `request_id`、`op`、
  `kind`、关键入参与结果，可与会话响应逐条按 id 对齐。
- [`examples/test-output.txt`](examples/test-output.txt)：`cargo test` 留存，
  24 个测试全部通过。
- 落盘索引 `demo.wmi` 为 172 字节，头部 `57 4d 58 42`(WMXB) + 版本 `01`，
  已验证：杀掉进程后用同一数据目录重启，`GET /indexes` 直接从磁盘读出索引且
  查询结果不变（构建→保存→重启加载一致性）。

夹具说明见 `fixtures/`（`basic.txt` 混合重复/负值、`all_same.txt` 全同值零层级、
`extremes.txt` 有符号极值）。

## 6. 失败类别速查

| kind | HTTP | 触发条件 |
| --- | --- | --- |
| `EMPTY_VALUES` | 400 | 用空序列构建索引 |
| `INVALID_JSON` | 400 | 请求体无法解析为端点 schema |
| `INVALID_INDEX_NAME` | 400 | 名字含非法字符 |
| `INDEX_ALREADY_EXISTS` | 409 | 已存在且未给 `overwrite` |
| `INDEX_NOT_FOUND` | 404 | 查询/删除不存在的索引 |
| `EMPTY_RANGE` | 422 | 查询区间 `l == r` |
| `K_OUT_OF_BOUNDS` | 422 | 0 基 `k >= r-l` |
| `RANGE_OUT_OF_BOUNDS` | 422 | `l > r` 或 `r > n` |
| `ROUTE_NOT_FOUND` | 404 | 未知路由 |
| `INDEX_FILE_*` | 500 | 落盘镜像损坏（magic/版本/长度/校验和/截断/结构） |
| `STORAGE_IO_ERROR` | 500 | 文件系统故障 |
