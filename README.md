# 分层位图集合（Layered Bitmap Set）

一套从零实现的分层（类 Roaring）32 位整数集合后端：固定阈值的**稀疏数组 / 稠密位图双容器**、
容器与集合两级的**并 / 交 / 差 / rank / select**、带 CRC32C 与严格校验的**二进制持久化格式**、
**Axum HTTP 验证接口**，以及不依赖被测实现的**独立参考实现与可复用夹具**。

整个仓库零外部账号、零网络依赖（依赖固定版本且已锁定，可离线构建），所有数据均为本地合成夹具。

## 模块划分（多模块 Cargo workspace）

| Crate | 职责 |
|---|---|
| `rb-format` | 数据格式与算法内核：容器、集合、rank/select、并交差、二进制编解码与校验、CRC32C |
| `rb-persist` | 文件系统持久化适配：原子写（临时文件 + fsync + rename）、目录存储、JSON 夹具导入 |
| `rb-server` | Axum 验证接口：统一可解释响应信封、请求身份关联、错误细分类别、写穿缓存 |
| `rb-testkit` | **独立**测试参考实现（仅用 `std::collections::BTreeSet`，不依赖 `rb-format`）、确定性 PRNG 与夹具 |
| `rbtool` | 夹具生成 / 文件结构检查 / JSON 导入的命令行工具 |

```
crates/
  rb-format/   src/{container,roaring,ops,codec,crc32c,error}.rs
  rb-persist/  src/{store,fixtures,naming}.rs
  rb-server/   src/{main,lib,handlers,state,error,model,request_id,config}.rs
  rb-testkit/  src/{oracle,rng,lib}.rs
  rbtool/      src/main.rs
fixtures/     rbtool 生成的可复用夹具（*.json + *.rbs + manifest.json）
scripts/      verify.py（独立 Python 验证器）、test_corruption.py、e2e.sh
```

## 核心契约与边界语义

- **整数空间**：`u32`，即 `0 ..= 4_294_967_295`。按高 16 位划分为至多 65 536 个容器，
  每个容器覆盖连续 65 536 个值（低 16 位）。
- **固定、有序、唯一的容器切换阈值**：容器基数 `≤ 4 096` 时规范为排序去重的
  `Array(Vec<u16>)`；`> 4 096` 时规范为 `Bitmap(1024 × u64)`。该阈值是常量
  `rb_format::ARRAY_MAX_CARDINALITY`，不随数据变化；任何运算结果都重新规范化，
  因此同一逻辑集合表示唯一（4 096 与 4 097 两侧边界都有测试）。
- **rank / select 语义**：
  - `rank(x)` = 集合中**严格小于** `x` 的元素个数（故 `rank(0) = 0`，
    `rank(u32::MAX)` 不计 `u32::MAX` 自身）；
  - `select(i)` = 第 `i` 小的元素（0 基），越界返回 `None`/`null` 并在 `notes` 说明；
  - 对集合内元素恒有 `select(rank(x)) = x`、`rank(select(i)) = i`（测试逐元素验证）。
- **最大整数边界不溢出**：基数与 rank 全部用 `u64` 累加（满集为 2³²，仍在 u64 内），
  编码长度/偏移用 `checked_*` 运算。
- **运算不退化为完整整数集合展开**：
  - 位图 × 位图：1 024 字的按位 `|` / `&` / `&!`，O(1024)；
  - 数组 × 数组：有序双指针归并，交集任一侧耗尽即终止；
  - 混合对：永远以小数组驱动位图查表，O(小数组长度)；
  - 集合级：按键有序双指针归并，缺失分片短路处理。
  只有夹具导出、结果分页等显式 I/O 路径才逐值产出（迭代器惰性产出，非运算前置展开）。

## 持久化格式（v1，魔数 `RBS1`）

详见 [`docs/FORMAT.md`](docs/FORMAT.md)。解码严格校验：魔数、版本、头部/体 CRC32C、
容器类型标签、键严格升序唯一、负载偏移（边界/顺序/无重叠）、负载长度（数组偶数、
位图恒 8 192 字节）、数组有序唯一、4 096 阈值、声明基数与实际 popcount 一致。
任一项不符都返回**分类明确**的 `CodecError`（HTTP 映射为 422 + 稳定错误码）。

## 快速开始（离线）

```bash
# 构建 / 测试（所有依赖版本在 Cargo.toml 中以 =x.y.z 锁定，Cargo.lock 已提交）
cargo build --offline --workspace
cargo test  --offline --workspace --all-features
cargo clippy --offline --workspace --all-targets --all-features   # 0 警告

# 生成可复用夹具到 fixtures/（JSON + 二进制 + manifest）
cargo run -q --offline -p rbtool -- gen-fixtures fixtures

# 检查一个 .rbs 文件（损坏时退出码非 0 并打印稳定错误类别）
cargo run -q --offline -p rbtool -- inspect fixtures/bitmap_just_above.rbs

# 启动 HTTP 服务（默认 127.0.0.1:8080，数据目录 ./data）
RB_BIND=127.0.0.1:8080 RB_DATA_DIR=./data cargo run -q --offline -p rb-server
```

## 一键验证脚本

```bash
./scripts/e2e.sh                      # 构建 + 全部 Rust 测试 + Python 独立验证 + HTTP 实测
python3 scripts/verify.py fixtures/   # 独立 Python 解析器交叉核对全部夹具
python3 scripts/test_corruption.py    # Python 独立构造 22 类损坏样本并断言错误码
```

`e2e.sh` 用临时目录与端口，结束自动清理；最后会打印 `PASS=n FAIL=0`。

## HTTP 接口摘要

所有响应都是统一信封：`request_id`（与 `x-request-id` 响应头一致，可用同名请求头指定）、
`format_version`、`result`、`errors[]`（失败原因单列）、`notes[]`（不确定/截断结论单列）、
`steps[]`（关键步骤与处理位置）。

| 方法与路径 | 说明 |
|---|---|
| `GET  /healthz` | 版本、阈值、容器位数 |
| `GET  /v1/sets` | 列出集合 |
| `POST /v1/sets?name=n` | 创建（body `{"values":[...], "expect_new":false}`） |
| `GET/PUT/DELETE /v1/sets/:name` | 查询汇总 / 整体替换 / 删除 |
| `GET/POST /v1/sets/:name/values` | 分页取值（`?limit=`，截断在 notes 说明）/ 追加值 |
| `GET /v1/sets/:name/contains/:v` | 成员判定 |
| `GET /v1/sets/:name/rank/:x` | rank（严格小于） |
| `GET /v1/sets/:name/select/:i` | select（越界返回 null + note） |
| `POST /v1/sets/:name/{union,intersect,difference}` | body `{"with":"other"}`，结果**不落盘**，steps 展示混合容器路径 |

失败状态码：404 `set_not_found`、409 `already_exists`、400 `invalid_name`/`invalid_json`、
422 `corrupt_*`（具体类别，如 `corrupt_body_checksum`、`corrupt_threshold_violation`）。

### 会话示例

```bash
curl -s -XPOST localhost:8080/v1/sets?name=a \
  -H 'content-type: application/json' -d '{"values":[1,2,3,100]}'
curl -s -XPOST localhost:8080/v1/sets/a/intersect \
  -H 'content-type: application/json' -d '{"with":"b"}'
curl -s localhost:8080/v1/sets/a/rank/3      # -> 2
```

## 验证策略（为什么不是“自己给自己判卷”）

1. **独立 oracle**：`rb-testkit` 不依赖 `rb-format`，全部参考答案用标准库 `BTreeSet`
   的数学集合定义给出；11 个夹具两两配对的并/交/差/子集/相交及逐点 rank/select 全量对照。
2. **独立 Python 验证器**：`scripts/verify.py` 重新实现格式解析、CRC32C、rank/select
   与集合运算，并与 JSON 夹具交叉核对。
3. **损坏拒绝**：Rust 与 Python 两侧分别独立构造 20+ 类损坏（截断、坏魔数、坏版本、
   头尾 CRC、未知标签、键乱序/重复、偏移越界、长度非法、数组乱序、阈值违反、基数不符），
   断言**具体错误类别**而非仅“调用失败”。
4. **断言具体结果**：阈值两侧的物理容器类型、并交差的具体基数、rank/select 具体值、
   交错夹具仅在共有分片相交等，均有硬断言。

夹具覆盖：空集、极稀疏、阈值边界（4096 / 4097）、稠密、全满容器、跨 ~1 900 个容器的
稀疏大集合、奇偶交错的混合容器对、u32 边界值（0 / 65535 / 65536 / u32::MAX）、
最高分片稠密集合。

## 未执行 / 不在范围内的检查（如实列出）

- **未做**并发写同一集合的悲观/乐观锁压测：当前 `put` 是“整集合读—改—原子覆盖”，
  并发追加到同一集合存在后写覆盖（last-write-wins）语义；不同集合之间互不影响。
  接口契约按单写者假设成立，这一点没有用测试证明强一致。
- **未做**真实断电下的 fsync 持久性验证（需要可注入故障的文件系统）；代码使用
  tmp+fsync+rename 的标准原子替换，崩溃时只会保留旧完整文件或新完整文件。
- **未做**性能基准/压测报告（无 criterion 等依赖，刻意保持依赖最小）；算法复杂度见上。
- HTTP 服务**未做**鉴权/TLS——只监听回环地址，定位为本地验证接口，不要直接暴露到公网。
- 32 位主机平台未实际运行测试（CI 仅在 x86_64 Linux）；代码用 u64 计数，逻辑上可移植，
  但该平台行为未实测。
