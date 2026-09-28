# mph-service

面向**不可变键集合**的最小完美哈希（Minimal Perfect Hash, MPH）构建与查询服务。

- 算法：BDZ 类 **3-一致超图剥离（graph peeling）** + 单调槽位分配
- 真实构建，随机种子按固定时间表重试，重试次数有上限
- 重复键在构建前去重并计数
- 映射在目标集合上是 `0..n-1` 的无碰撞排列
- **集合外查询不会被误报为成员**：候选槽位上的 64 位指纹必须验证通过
- 持久化格式绑定哈希种子、格式版本与算法版本，CRC 覆盖头部与载荷
- 技术栈：Rust + Axum + 本地文件系统，无生产账号、无外部服务依赖

## 目录结构

```
src/
  hash.rs          哈希原语（splitmix64 / FNV-1a，跨语言规范）
  kernel/
    graph.rs       3-一致超图边构建与剥离（peeling）
    assign.rs      按剥离栈逆序分配 g[] 与槽位
    builder.rs     去重 + 种子重试上限的构建驱动
    index.rs       查询内核：候选槽位 + 指纹验证
  format.rs        二进制持久化格式（魔数/版本/种子/CRC）
  store.rs         文件系统适配（tmp + rename 原子写）
  config.rs        JSON 配置
  diag.rs          请求标识与决策记录（脱敏）
  api.rs           Axum 验证接口
  main.rs          服务入口
tests/             独立测试（内核/格式/参考向量/HTTP）
tests/fixtures/    Python 参考实现生成的期望向量
scripts/gen_reference.py  独立于 Rust 内核的参考实现
config/service.json
```

## 依赖版本

见 `Cargo.toml`（`cargo tree` 可展开完整锁定树）：

| crate | 版本 |
|---|---|
| rust edition | 2021（rustc 1.98 验证） |
| axum | 0.8 |
| tokio | 1（full） |
| serde / serde_json | 1 |
| thiserror | 2 |
| tracing / tracing-subscriber | 0.1 / 0.3 |
| tower（仅 dev） | 0.5 |

## 从零复现

```bash
# 1. 生成独立参考向量（需要 python3，仅标准库；fixture 已随仓库提交）
python3 scripts/gen_reference.py

# 2. 构建
cargo build --release

# 3. 全量测试
cargo test

# 4. 启动（默认读 config/service.json，也可传入路径参数）
./target/release/mph-service config/service.json
```

配置（`config/service.json`）：

```json
{
  "listen": "127.0.0.1:8080",
  "index_path": "data/index.mph",
  "default_seed": 1592079361,
  "default_max_attempts": 128,
  "max_keys": 1000000
}
```

## HTTP 接口与请求样例

### 构建索引（去重 + 有界重试 + 落盘）

```bash
curl -s -X POST 127.0.0.1:8080/v1/index/build \
  -H 'content-type: application/json' \
  -d '{"keys":["alpha","bravo","charlie","alpha"],"seed":42,"max_attempts":128}'
# {"request_id":"req-...","status":"ok","n":3,"m":4,"seed":42,
#  "attempts":1,"duplicates_removed":1,"persisted_to":"data/index.mph"}
```

失败时返回 `422` 与具体类别：`peeling_exhausted` / `too_many_keys`。

### 查询（指纹验证后的成员判定）

GET（适合简单 ASCII 键）：

```bash
curl -s '127.0.0.1:8080/v1/query?key=bravo'
# {"request_id":"req-...","key_id":"fp12=....,len=5",
#  "decision":"member","slot":2,
#  "reason":"fingerprint verified at candidate slot","index_seed":42,"index_n":3}

curl -s '127.0.0.1:8080/v1/query?key=not-a-key'
# {"...":"...","decision":"not_member",
#  "reason":"fingerprint mismatch at candidate slot 0; key is not in the indexed set", ...}
```

POST JSON（二进制安全，含空格/中文/emoji/控制字符的键建议用此方式）：

```bash
curl -s -X POST 127.0.0.1:8080/v1/query \
  -H 'content-type: application/json' -d '{"key":"中文键 with space"}'
```

`decision` 取值：

- `member`：槽位上的指纹验证通过，返回 `slot`
- `not_member`：空索引，或候选槽位指纹不匹配（附 `candidate_slot` 说明拒绝原因）
- `undecidable`：没有加载任何索引，系统状态无法判定

日志与响应**只包含脱敏键标识**（指纹前 12 位 + 长度），不打印原始键。

### 重载 / 健康检查 / 统计

```bash
curl -s -X POST 127.0.0.1:8080/v1/index/load -d '{}'      # 从 index_path 重载
curl -s 127.0.0.1:8080/v1/health
curl -s 127.0.0.1:8080/v1/stats
```

请求标识：传入 `x-request-id` 头会原样使用并在响应头/体回显；不传则生成 `req-<毫秒时间>-<计数器>`。

## 持久化格式

见 `src/format.rs` 顶部注释。魔数 `MPHFBDZ\x01`，头部 56 字节含格式/算法版本、种子、n、m、载荷长度与 CRC；
CRC 链式覆盖头部前 48 字节与全部载荷，篡改种子或 `g[]` 都会得到 `CrcMismatch`。

## 算法与正确性要点

- 顶点数按规模分档：`n=0 → m=0`；`n≤64 → m=max(3,2n)`；`n≤2048 → m=ceil(1.5n)`；否则 `m=ceil(1.23·n)`（小集合剥离阈值收敛慢，需加宽预算）；每键由种子派生 3 个顶点哈希，顶点不互异则该次种子作废重试
- 剥离队列反复移除度为 1 的顶点及其最后一条边；剩边非空（2-核）则换种子
- 逆剥离顺序赋值：槽位按 `0,1,2,…` 单调发放，解出临界顶点的 `g`，保证最小且无碰撞
- 查询：`slot = (g[v0]+g[v1]+g[v2]) mod n`，再与槽位指纹比较（64 位，误报率约 2⁻⁶⁴）

哈希规范及与参考实现的一致性见 `docs/HASH_SPEC.md`。

## 测试命令与断言内容

```bash
python3 scripts/gen_reference.py   # 重新生成独立期望向量
cargo test                         # 全部独立测试
```

- `tests/kernel_tests.rs`
  - 对 `n = 0..=40` 的集合穷举断言槽位排序后恰为 `0..n-1`
  - 重复键计数与顺序、空集合构建/拒绝查询
  - 用合成 K4 三一致 2-核断言剥离失败类别（`peeled 0 of 4`）
  - 扫描真实失败种子：`max_attempts=1` 返回 `PeelingExhausted`；放开预算后**断言恰好在第 2 次成功**
  - 676 个集合外键 + 成员键扰动全部被拒绝，无假阳性
- `tests/format_tests.rs`：截断 / 魔数 / 版本 / 载荷篡改 / 种子篡改（CRC）/ 长度不符 / 空索引往返
- `tests/reference_vectors.rs`：Rust 内核必须与**独立 Python 实现**生成的 `g[]`、槽位、指纹逐位一致
- `tests/api_tests.rs`：端到端 HTTP，断言具体 decision、reason、错误类别、request-id 回显、重载后行为一致

实际执行结果记录在 `RESULTS.md`。
