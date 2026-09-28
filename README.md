# LZ77B — 带滑动字典的 LZ77 块压缩后端

固定参数的 LZ77 块压缩后端，支持**独立块**（字典为空）与**依赖前块**（绑定前置滚动
字典摘要）两种模式。Rust 实现，Axum 提供 HTTP 校验/服务接口，文件系统持久化，全部
夹具本地合成，无外部账号或真实业务数据依赖。

## 工程边界

```
src/
├── core/                  数据格式 + 编解码内核（纯逻辑，无 I/O、无 async）
│   ├── error.rs           四类错误契约：input / state / resource / compute
│   ├── constants.rs       固定线格式常量（窗口、匹配范围、各项上限）
│   ├── checksum.rs        CRC-32/IEEE（载荷完整性）+ FNV-1a（字典摘要）
│   ├── varint.rs          严格无符号 LEB128
│   ├── format.rs          30 字节定长头 + Token 流语法
│   ├── encoder.rs         哈希链贪心编码器（匹配可跨前块字典）
│   └── decoder.rs         逐字节重叠复制解码器 + ChainSession 链式规则
├── reference/             独立参考解压器（与 core 零代码共享，各自重写头部/
│                          变长整数/CRC/重叠复制/错误分类），用于交叉验证
├── store/                 文件系统持久化适配（原子写、重扫索引、总量上限）
├── http/                  Axum 路由与处理器（错误码→HTTP 状态、run id 传播）
├── util_b64.rs            JSON 二进制字段用的最小 base64
└── bin/{server,cli}.rs    服务端与命令行

tests/evidence_01..06_*.rs 六组证据测试（29 个集成用例）
tests/common/recorder.rs   可重放的运行记录器（run id / 中间状态 / 判定理由）
examples/gen_fixtures.rs   合成夹具与手工块生成器（不经过被测编码器）
fixtures/                  最小数据夹具（sample1.bin + 3 个手工链块）
docs/                      线格式、复现文档、冒烟证据
```

模块间只通过显式契约通信：`core::error::Error { code, category, detail }`
贯穿内核、存储与 HTTP；存储层把类别映射为 400/409/413/500。没有空壳文件。

## 关键正确性点

- **固定参数，不可协商**：窗口 4096、最小匹配 3、最大匹配 65538、单载荷 ≤64 KiB、
  单块输出 ≤1 MiB，攻击者无法通过线格式索要更大窗口。
- **重叠复制逐字节语义**：`length > distance` 时按 DEFLATE 语义对“不断增长的
  `字典++输出` 历史”逐字节索引，绝不切片复制尚不存在的字节。
- **依赖块绑定前置字典摘要**：依赖块携带 `FNV1a(index ‖ len ‖ dict)`；链式会话
  校验索引连续性与摘要，摘要不符报 `digest_mismatch`（state 冲突），与 CRC
  错误（input）可区分。
- **解压多重预算**：先按头部声明长度（受绝对上限和倍率上限约束）分配，每次追加
  再做逐字节预算检查；声明 4 GiB 的炸弹在头部即被拒，RSS 增量为 0（见
  `test-results/runs/04-output-cap-*.log`）。整链解码另有独立的**流解压聚合上限**
  （默认 16 MiB，`stream_output_cap_exceeded`），防止一连串高比率小块在一次
  请求里放大成 GB 级内存。
- **参考答案独立**：`src/reference` 从格式文档独立重写，不由被测核心生成；
  `/v1/decode?cross_check=true` 与全部证据测试都要求两者字节一致。

## 快速开始

```bash
cargo build --release --offline        # 依赖已在 Cargo.lock 锁定

# 命令行
./target/release/lz77b compress fixtures/sample1.bin /tmp/s.lzb
./target/release/lz77b verify   /tmp/s.lzb            # core 与参考解压器互验
./target/release/lz77b chain    fixtures/sample1.bin /tmp/c 512   # 多块链
./target/release/lz77b unchain  /tmp/c /tmp/out.bin              # 链解码
cmp fixtures/sample1.bin /tmp/out.bin

# 服务
./target/release/lz77b-server --listen 127.0.0.1:8080 --store ./lz77b-store
bash examples/http-calls.sh            # 正常 + 异常调用回放
```

## 测试与证据

```bash
cargo test --offline                   # 26 单元 + 29 集成证据用例（55 个）
```

每次运行写入：

- `test-results/runs/<run-id>.log`：编号事件、关键中间状态、期望值/实际值/判定理由；
- `test-results/summary.jsonl`：每个用例一行结论（含异常中断时由 Drop 落盘的 FAIL）。

| 证据文件 | 覆盖 |
|---|---|
| `evidence_01_codec.rs` | 距离 1/2 自重叠长匹配、跨窗口边界、跨块边界、最大匹配、错误距离 |
| `evidence_02_chain.rs` | 缺前块、摘要错配、索引跳号/重复、CRC/长度/魔数损坏、参考器分类 |
| `evidence_03_equivalence.rs` | 同输入在 1 块与 16/100/1024/4096 多种分块下字节相同；手工夹具 |
| `evidence_04_resource.rs` | 输出/倍率/载荷/总量四类耗尽可区分，4 GiB 炸弹零 RSS 增长 |
| `evidence_05_store.rs` | 持久化重扫、落盘篡改检测、路径穿越拒绝、乱序拒绝 |
| `evidence_06_http.rs` | run id 传播、端到端、四类错误→400/409/413、请求体上限 |

完整复现步骤与已留存的真实运行结果见 [`docs/REPRODUCING.md`](docs/REPRODUCING.md)，
线格式细节见 [`docs/FORMAT.md`](docs/FORMAT.md)。

## 许可

MIT。
