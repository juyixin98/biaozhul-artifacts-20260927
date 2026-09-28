# 验收结果记录（RESULTS.md）

执行日期：2026-09-27（UTC）。环境：Linux 6.8、cargo/rustc 1.98.1、Python 3.12.3（仅标准库）。
以下命令均在仓库根目录实际执行，结果如实记录。

## 1. 独立参考向量（不由被测内核生成）

```
$ python3 scripts/gen_reference.py
wrote .../tests/fixtures/reference_vectors.json with 6 cases
```

参考实现为独立 Python 重写（`scripts/gen_reference.py`），与 Rust 内核共享
`docs/HASH_SPEC.md` 描述的规范，输出 6 个用例（空集、单键、8 词集、含重复键、
含空串/空白等二进制安全键、15 个数字键）的期望 `seed / m / g[] / slots / fingerprints`。

手工核对过一个中间值，确认两侧逐位一致：

```
keyed64("aa", 0x1122334455667788) = 0x6a358d2c0c4ba773   (Python 与 Rust 相同)
vertex_hash(seed=1, i=0..2, "aa") = adc46763... / d9d234e3... / 87964cee...（两侧相同）
```

## 2. 构建（含下载依赖）

```
$ cargo build --release
    Finished `release` profile ...
$ ls -l target/release/mph-service
-rwxrwxr-x ... 3603536 ... mph-service
```

主要依赖版本（Cargo.toml 声明，实际版本锁定于 Cargo.lock）：
axum 0.8（实际 0.8.9）、tokio 1、serde 1、serde_json 1、thiserror 2（2.0.21）、
tracing 0.1、tracing-subscriber 0.3（0.3.23）；dev-dependency tower 0.5。

## 3. 全量测试

```
$ cargo test
test result: ok. 0 passed; 0 failed;   (lib 单测)
test result: ok. 0 passed; 0 failed;   (main)
test result: ok. 9 passed; 0 failed;   tests/api_tests.rs
test result: ok. 8 passed; 0 failed;   tests/format_tests.rs
test result: ok. 9 passed; 0 failed;   tests/kernel_tests.rs
test result: ok. 1 passed; 0 failed;   tests/reference_vectors.rs
```

合计 27 个断言全部通过；`cargo clippy --all-targets` 零警告。

测试覆盖的关键断言（不是“接口能调用”式检查）：

- **穷举排列**：n = 0..=40 的每个集合，成员槽位排序后严格等于 `0..n-1`。
- **大规模真实构建**：n = 5000（1.23 渐近档），无碰撞完整排列 + 500 个集合外键全拒绝。
- **重复键**：先去重并计数（输入 8 键、4 个重复 → n=4，duplicates_removed=4）。
- **空集合**：成功构建，查询以 `EmptyIndex` 类别拒绝。
- **剥离失败分类**：合成 K4 三一致 2-核断言 `peeled 0 of 4 edges`；
  并扫描到真实的“第 0 次尝试留 2-核、第 1 次成功”的种子，断言
  `max_attempts=1 → PeelingExhausted{attempts:1}`，放开预算后 `attempts == 2`。
- **非成员拒绝**：676 个双字符外键 + 成员键逐位扰动，全部
  `FingerprintMismatch`，无假阳性。
- **格式**：截断 / 坏魔数 / 格式版本 / 算法版本 / 载荷篡改 / **种子篡改**（CRC 覆盖头部）
  / 长度不符，分别断言具体错误类别与数值。
- **参考向量一致性**：Rust 的 `g[]`、槽位、指纹必须逐位等于独立 Python 实现的输出。
- **HTTP**：成员/非成员/无法判定三类 decision 及具体 reason、422+`peeling_exhausted`、
  `bad_request`、request-id 自定义头回显、落盘后重载行为一致、Unicode/二进制安全查询、
  脱敏 key_id 形状（`fp12=…,len=…`）。

## 4. 真实服务冒烟（release 二进制）

启动（`./target/release/mph-service /tmp/mph-smoke/config.json`），构建 500 个
合成键 + 2 个重复：

```
POST /v1/index/build {"keys":[500 keys + 2 dups], "seed":20260928}
-> {"status":"ok","n":500,"m":750,"seed":20260937,"attempts":10,
    "duplicates_removed":2,"persisted_to":"/tmp/mph-smoke/index.mph"}
```

- 经 API 逐个查询 500 个成员 → 槽位集合恰为 `{0..499}`（脚本断言通过）。
- 外键（`user-00500…`、空串、`admin@example.test`、大小写变体）全部 `not_member`。
- 强制失败：4 键 + 坏种子 + `max_attempts:1` → HTTP 422 `peeling_exhausted`。
- **重启进程**后自动从磁盘加载（日志 `loaded persisted index ... n=500 seed=20260937`），
  `user-00000@example.test` 重载前后槽位均为 264。
- **脱敏审计**：对两份服务日志执行
  `grep -E "secret|敏感|user-[0-9]|example\.test|中文"` → 无任何命中，
  日志只有 `key_id=fp12=<12hex>,len=<n>` 与 request_id、decision、reason。

## 5. 复现步骤摘要

```bash
python3 scripts/gen_reference.py     # 生成独立期望向量（fixture 已随仓库提交）
cargo build --release
cargo test                           # 27 个测试
cargo clippy --all-targets           # 零警告
./target/release/mph-service config/service.json
# 另见 README.md 的 curl 请求样例
```

## 6. 过程中发现并修复的真实问题（留痕）

1. **弱哈希导致退化边风暴**：最初顶点哈希用 FNV-1a，其乘法递推在短而相近的键上
   低位雪崩极差（`aa/bb/cc/dd` 模 4 的三个位置全部相同），产生大量退化超边。
   改用分块 splitmix64 混合（小模数下仍有良好雪崩），Rust 与 Python 两侧同步。
2. **小集合不可剥离**：n=3、m=4 等组合在结构上必然留下 2-核。最终采用分档顶点规则
   `n≤64→2n`、`n≤2048→1.5n`、否则 `1.23n`；网格仿真
   （n=0..120 全尺寸×多种子 + 200..10000 抽点）0 次构建失败，最坏 98 次尝试
   （默认预算 128）。
3. **指纹按错误的下标存储**：初版按“键顺序”存指纹而查询按槽位取，导致成员被误拒；
   修正为 `fps[slot(key)] = fingerprint(key)`，该错误被穷举排列与参考向量测试捕获。
4. **种子未被完整性校验覆盖**：CRC 最初只覆盖载荷，改为链式覆盖头部前 48 字节
   （含种子与版本），篡改种子即 `CrcMismatch`。
