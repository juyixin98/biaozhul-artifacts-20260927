# fm-index-service

字节文本（`&[u8]`，含二进制零字节）的 **FM 索引**纯后端服务：BWT + Occ/rank 检查点 +
后缀数组位置采样 + LF 定位，Rust / Axum / 本地文件系统持久化。无外部服务依赖，
全部测试数据为本地合成夹具。

---

## 1. 它实现了什么

| 需求 | 实现位置 | 说明 |
|------|----------|------|
| 哨兵唯一、不与正文冲突 | `src/alphabet.rs` | 正文 `byte → byte+1`（1..=256），哨兵固定 `0`；0x00 正文是符号 1 |
| BWT | `src/suffix_array.rs` + `src/bwt.rs` | 倍增+计数排序 O(n log n) 构造 SA；`L[i]` 由 SA 导出 |
| rank 结构 | `src/rank.rs` | 每 `rank_block` 个位置一张 257 项全检查点；`rank_c(i)` O(block) |
| 后向搜索半开区间 | `src/fm.rs::search_traced` | 返回 `[lo, hi)`，空区间 `lo==hi`；可输出每步中间状态 |
| LF 映射终止定位 | `src/fm.rs::locate_row` | `LF(i)=C[L[i]]+rank`；沿 LF 走到 SA 采样行，`pos=sa+步数` |
| 空模式 / 超长模式 | `src/fm.rs` | 空模式 `count=n`、`locate=0..=m`；超长模式显式空区间 |
| 持久化 + 损坏检测 | `src/persist.rs` | 暂存目录原子 rename + manifest SHA-256 + 结构自校验 + 内核 validate |
| 朴素扫描参照 | `src/naive.rs` | 独立实现，只供测试与 `/verify`，**不参与查询路径** |
| 验证接口 | `GET/POST /v1/indexes/:name/verify` | FM 结果与朴素扫描逐项并列对照 |
| 错误分类 | `src/error.rs` | invalid_input / not_found / state_conflict / resource_exhausted / corrupt / computation_failed |
| 运行编号 | `src/api/mod.rs` | 每请求 `run-<pid>-<ns>-<seq>`，可经 `x-run-id` 指定，日志与响应都带 |

### 模块边界（数据与错误契约）

```
src/
  alphabet.rs      编码格式：字节 <-> 257 符号，哨兵 0；编码序列校验
  suffix_array.rs  SA 构造（输入 &[u16]，输出 Vec<u32>）
  bwt.rs           BWT 列与 C 频次表
  rank.rs          Occ 检查点 + 自带二进制编解码与自校验
  fm.rs            FmIndex 内核：搜索/LF/定位/不变量 validate
  naive.rs         独立朴素扫描参照（测试 oracle，非被测实现自身）
  persist.rs       文件布局、原子提交、哈希校验、损坏分类、list/remove
  service.rs       IndexService：注册表、建索引、导入白名单、查询、verify
  config.rs        TOML/CLI/默认配置
  api/             Axum：路由、统一错误信封、运行编号中间件、JSON DTO
  error.rs         唯一错误类型 FmError + 6 类 ErrorKind + HTTP 映射
```

内核层不知道 Axum，HTTP 层不接触 BWT 细节；跨层只传 `FmError` 与几个 DTO。

---

## 2. 快速开始

需要 Rust 1.98+（edition 2024）。

```bash
cargo build --release
./target/release/fm-index-service --config config.toml
# 或开发模式：cargo run
```

默认监听 `http://127.0.0.1:8921`，数据目录 `./data/indexes`，导入白名单 `./examples/data`。
命令行参数：`--host/-H`、`--port/-p`、`--data-dir/-d`、`--config/-c`；`--help` 查看全部。

### 端到端示例（实际输出）

```bash
$ curl -s -X POST localhost:8921/v1/indexes -d '{"name":"banana","path":"banana.txt"}'
{"run_id":"run-...","data":{"name":"banana","encoded_len":7,"text_len":6,
 "rank_block":256,"sample_step":16,"sample_count":1,"created_at":1790525688}}

$ curl -s "localhost:8921/v1/indexes/banana/search?pattern=ana"
{"run_id":"run-...","data":{"pattern_len":3,"pattern_base64":"YW5h","count":2,
 "interval":{"lo":2,"hi":4,"half_open":true},"locations":[1,3]}}
```

`ana` 在 `banana` 的 **重叠** 位置 1 和 3 都被报告；区间为半开 `[2,4)`。

二进制零字节文本用 base64 传模式（`00 00` → `AAA=`）：

```bash
$ curl -s -X POST localhost:8921/v1/indexes/zeros/search -d '{"pattern_base64":"AAA="}'
{"run_id":"...","data":{"count":119,"interval":{"lo":...,"hi":...,"half_open":true},
 "locations":[0,1,2,3,4,5,6,7,...]}}

$ curl -s -X POST localhost:8921/v1/indexes/zeros/verify -d '{"pattern_base64":"/w8="}'
{"run_id":"...","data":{"fm_count":0,"naive_count":0,"agree":true,...}}
```

### HTTP 接口

| 方法 | 路径 | 作用 |
|------|------|------|
| GET | `/v1/health` | 状态 + 已加载/磁盘索引列表 |
| GET | `/v1/indexes` | 列出 loaded / on_disk |
| POST | `/v1/indexes` | 建索引（`text` / `text_base64` / `path` 三选一，可带 `rank_block`,`sample_step`） |
| GET | `/v1/indexes/:name` | manifest 元信息 |
| DELETE | `/v1/indexes/:name` | 删除 |
| POST | `/v1/indexes/:name/load` | 冷加载到内存 |
| GET/POST | `/v1/indexes/:name/search` | count + 半开区间 + locations；POST 可 `trace:true` |
| GET | `/v1/indexes/:name/count` | 只计数 |
| GET/POST | `/v1/indexes/:name/verify` | FM vs 朴素扫描对照 |

模式传参：`pattern`（UTF-8 字符串）或 `pattern_base64`（任意字节）；空模式用
`empty=true`（GET 缺省也视为空模式）。响应统一包 `{"run_id","data"}`，错误为
`{"run_id","error":"<kind>","message"}`。

### 错误分类 → HTTP

| error kind | HTTP | 触发示例 |
|------------|------|----------|
| `invalid_input` | 400 | 空文本、步长 0、坏 base64、坏 JSON、非法索引名、磁盘有但未加载 |
| `not_found` | 404 | 索引不存在（业务 404，消息含索引名）；路由不存在（框架 404） |
| `state_conflict` | 409 | 同名索引已存在 |
| `resource_exhausted` | 413 | 文本超 `max_text_bytes`、命中数超 `max_locations`、请求体超限 |
| `corrupt` | 500 | 持久化文件缺失/截断/哈希不符/结构自校验失败/内核不变量破坏 |
| `computation_failed` | 500 | LF 行走超步未入样等内部不变量错误、I/O 错误 |

---

## 3. 运行测试（保留真实命令与结论）

```bash
cargo test
```

最近一次结果（开发机，debug）：

```
running 21 tests (src 单元)  ... test result: ok. 21 passed; 0 failed
tests/fm_core.rs    (8 个)  ... test result: ok. 8 passed; 0 failed
tests/persistence.rs (5 个) ... test result: ok. 5 passed; 0 failed
tests/api_server.rs (7 个)  ... test result: ok. 7 passed; 0 failed
合计 41 passed; 0 failed
```

- **单元测试**：编码唯一性、SA 对朴素排序（含 500 组小字母表随机）、BWT/C、
  rank 对朴素前缀计数（多种 block）、内核语义、编解码截断拒绝。
- **`tests/fm_core.rs`**：硬编码 banana 具体答案；高重复重叠匹配；0x00/0xff 二进制；
  空模式/超长模式；**5 种 rank_block × 9 种 sample_step × 9 模式**参数扫描全对照朴素；
  2 万字节文本端到端；trace 中间区间。
- **`tests/persistence.rs`**：保存/冷加载一致；**10 种文件篡改**（occ/c/samples/text/manifest
  的截断、翻转、追加）全部归类 `corrupt`；删数据文件=`corrupt`，删 manifest=`not_found`；
  绕过哈希层篡改采样仍被内核 `validate` 抓出；重复创建=`state_conflict`；
  启动时损坏索引被隔离而好索引可用。
- **`tests/api_server.rs`**：生命周期、具体命中位置、base64 二进制、verify、
  400/404(业务 vs 框架)/409/413 可区分、`x-run-id` 回显、未加载状态提示、白名单路径逃逸。

> 测试期望来自两类**独立**来源：人工硬编码夹具（banana 等）和 `src/naive.rs`
> 的原文线性扫描，不由被测 FM 内核自身生成答案。

### 可重放的测试日志

每个集成用例通过 `tests/common/mod.rs::TestLog` 把
`运行编号 + 夹具描述 + 关键中间状态（区间 [lo,hi)、计数、trace、manifest、错误类别）+ 判断理由`
实时（带 `fsync`）追加到：

```
test-logs/<test-binary>/<test-name>.log
```

即使断言 panic，之前的中间状态已落盘。日志中的 `it-<pid>-<ns>-<seq>` 即运行编号，
配合夹具（确定性 LCG `pseudo_bytes(seed,len,alpha)`）可完整重放。
（该目录已在 `.gitignore`。）

```bash
cargo test --test fm_core -- --nocapture   # 日志同时打到 stdout
```

---

## 4. 样例数据

`examples/data/`：

| 文件 | 内容 | 用途 |
|------|------|------|
| `banana.txt` | `banana` | 教科书 BWT/重叠匹配 |
| `high_repeat.txt` | `ab`×40 + 句子 | 高重复、周期模式 |
| `binary_zeros.bin` | 128 字节，大量 0x00 夹杂 0xff/0x01 | 哨兵与零字节不冲突 |
| `words.txt` | 英文短语 | 常规多模式查询 |

导入受 `storage.import_dirs` 白名单约束，`..` 逃逸在服务端被拒绝。

---

## 5. 持久化格式

每个索引目录 `data/indexes/<name>/`：

```
manifest.json   提交点（最后写入）；含 format_version、参数、created_at、各文件 sha256+len
occ.bin         magic "OCC1" | block | n | BWT(u16×n) | 检查点表(u32×257×(nblocks+1))
c.bin           C 表 257×u64
samples.bin     count(u64) | (row u64, sa u64)×count
text.bin        原始字节
```

保存走“暂存目录 → 各文件 fsync → 目录 fsync → rename → manifest 已就位”，
崩溃不会留下“看似完整”的半成品（无 manifest 即视为不存在）。加载执行
长度校验 → SHA-256 → Occ 自洽重建 → 内核 LF 双射/哨兵唯一/采样整除等终检。

---

## 6. 语义约定（避免歧义）

- 原文长度 `m`，编码长度 `n=m+1`；位置取值 `0..=m`。
- 后向搜索区间是**半开** `[lo,hi)`，命中数 `hi-lo`，无命中 `lo==hi`。
- 空模式：`count=n`，`locate={0,…,m}`（m+1 个间隙）。
- 超长模式（`p.len()>m`）：不允许跨哨兵匹配，直接返回空区间。
- `locate` 返回全部**重叠**匹配起点（如 `aaaa` 中 `aa` 为 `[0,1,2]`）。
