# 分层位图集合（Hierarchical Bitmap Set）

一套多模块 Rust 后端：固定阈值的分层位图集合内核、二进制序列化与校验、
文件系统持久化、Axum 验证接口、独立参照实现与可复用夹具。

- 内核不使用任何外部依赖；参照实现（`hbs-testkit`）只用标准库
  `BTreeSet`，与被测核心不共享任何算法代码。
- 集合运算按 chunk / 容器就地执行，**不会**把集合展开为完整整数向量
  （只有显式迭代器才惰性产出值）。

## 模块划分

| Crate | 职责 |
|---|---|
| `hbs-core` | 索引内核：稀疏 `Array`、稠密 `Bitmap` 容器，固定切换阈值，并/交/差/对称差，`rank`/`select`，惰性迭代 |
| `hbs-format` | 二进制格式 v1：魔数/版本/标志、目录（键、类型、基数、偏移）、CRC-32；解码时逐项校验 |
| `hbs-store` | 文件系统适配：`<name>.hbs`，临时文件 + fsync + 原子 rename，名称校验防路径穿越 |
| `hbs-config` | 无依赖配置文件解析 + `HBS_*` 环境变量覆盖，错误有具体类别 |
| `hbs-testkit` | 独立 `BTreeSet` 参照实现、确定性数据生成（稀疏/稠密/交错/阈值）、性质校验、夹具落盘 |
| `hbs-server` | Axum 验证接口：请求 ID 关联、步骤轨迹、失败类别、不确定结论单列 |
| `hbs-cli` | `hbs` 命令：`serve` / `gen-fixtures` / `verify` / `inspect` |
| `hbs-tests` | 独立集成测试：断言**具体结果与失败类别** |

## 快速开始

```bash
cargo test --workspace        # 全部测试（含跨实现对照与损坏拒绝）
cargo build --release

# 生成可复用夹具（.values.json 为真值，.hbs 为编码产物，.meta.json 为期望元数据）
cargo run -p hbs-cli -- gen-fixtures --out tests/fixtures/generated

# 启动验证接口
cargo run -p hbs-cli -- serve --config hbs.conf

# 校验单个文件（退出码：0 正常，3 不存在，4 损坏）
cargo run -p hbs-cli -- verify --data-dir data my-set
```

一键验证脚本：`scripts/verify.sh`（构建、测试、夹具生成、起服务、HTTP
冒烟、损坏注入与退出码检查）。

## 边界语义（重要）

- **论域**：全部 `u32` 整数 `[0, 2^32)`。`0` 与 `u32::MAX` 均可存可取。
- **分块**：高 16 位为 chunk key（65,536 个），低 16 位为块内位置
  （65,536 个槽位）。
- **固定切换阈值**：块内基数 `<= 4096` 用稀疏有序数组；`> 4096`
  （4097..=65536）用稠密 1024×u64 位图。删除使基数回到阈值时，稠密
  容器**会退回**数组；4096 恰在数组一侧。表示因此确定且唯一。
- **有序唯一**：所有容器内容严格递增、无重复；构造与解码都校验。
- **基数不溢出**：容器基数 `usize`（≤65,536）；集合总基数与 `rank`
  返回 `u64`（全集 2^32 也不会溢出）。`select` 接受 `u64` 排名，
  `rank >= len` 返回 `None` 而不是 panic 或回绕。
- **rank 两种端点**：`rank_lt(v)` = `v` 严格小于的成员数（`0..`）；
  `rank_le(v)` = 小于等于（`..=v`）。“`u32::MAX+1` 的排名”不存在也
  不需要：全集大小用 `len()`，端点包含用 `rank_le(MAX)`。
- **rank/select 互逆**：对任意成员 `v`，`rank_lt(v) = r ⇒ select(r) = v`；
  `rank_le(select(r)) = r + 1`。测试对稀疏/稠密/交错/阈值夹具逐点验证。
- **空集合**：允许，编码为仅头部的 28 字节文件；空 chunk 不落盘。
- **集合运算**：并/交/差/对称差按 chunk 对齐执行；数组×数组做归并，
  位图×位图做逐字位运算，混合型收集进位图；结果重新规范化（可能在
  表示间切换）。子集判断逐块完成，不展开全集。

## 序列化格式 v1

小端序，布局见 `hbs-format/src/lib.rs` 顶部文档注释：

```text
header 28B: magic "HBS1" | version=1 | flags=0 | num_chunks
           | dir_offset=28 | dir_len | data_len | crc32(其余全部字节)
directory: 每 chunk 16B = key u16 | kind u16(1数组/2位图)
                    | cardinality u32 | data_offset u32 | reserved=0 u32
payload:   数组 = cardinality*u16（严格递增，<=4096）
           位图 = 1024*u64（popcount 必须等于 cardinality，>4096）
```

解码按固定顺序拒绝并给出**具体类别**：
`Truncated / BadMagic / UnsupportedVersion / BadFlags / BadHeader /
ChecksumMismatch / BadDirectory / UnknownContainerKind /
Cardinality{..} / ArrayNotSorted / TrailingBytes`。

校验内容包括：魔数、版本、保留位/保留字段为零、头部长度自洽、
CRC-32、目录键严格递增、偏移严格首尾相接且恰好覆盖数据区、容器类型
合法、基数与阈值一侧一致且与实际内容（数组元素数 / 位图 popcount）
相等、数组严格递增、无尾随字节。

## HTTP 接口（`/api/v1`）

每个响应信封都带 `request_id`（尊重 `X-Request-Id` 头，否则生成
UUID）、`version`、`steps`（关键步骤与处理位置）、`uncertainties`
（不确定结论，单独字段）、以及失败时的 `error_code`。

- `GET  /health`
- `GET  /sets`、`POST /sets`、`GET|DELETE /sets/{name}`
- `POST /sets/{name}/rank` `{value, inclusive?}`
- `POST /sets/{name}/select` `{rank}`（越界为 200 + `found:false`
  + uncertainties，而不是报错）
- `POST /sets/{name}/contains`
- `GET  /sets/{name}/verify`（损坏 → 422 + `corrupt_*` 具体类别）
- `POST /algebra/{union|intersection|difference|symmetric-difference}`
  `{left,right,save_as?}`（不给 `save_as` 时结果不落盘，写入 uncertainties）
- `POST /verify/cross-check` `{a,b}` —— 与独立 `BTreeSet` 参照实现对账
- `POST /fixtures` `{distribution}` —— 对确定性夹具批量跨实现校验

## 验证方案如何满足“参考答案不能由被测核心自身生成”

`hbs-testkit::ReferenceSet` 是独立的 `std::collections::BTreeSet`
封装：成员、基数、rank（`range` 计数）、select（`nth`）、并交差全部
由标准库集合语义给出；夹具真值文件 `.values.json` 是生成的原始输入，
从不通过解码被测文件产生。测试把内核答案逐项与该参照对账，并单独
注入字节损坏断言具体失败类别。

## 无法在本环境执行 / 未覆盖的检查（如实列出，不声称通过）

- 未做真实掉电/崩溃注入；原子写依赖目标文件系统 rename 原子性与
  fsync 语义（本地 ext4/tmpfs 成立，网络文件系统需另行验证）。
- 未做多进程并发写同一集合的锁协调（当前为单进程服务场景）。
- 未做性能基准/压测断言；功能正确性为主。
- 未进行依赖的安全审计（依赖数量少且版本在根 `Cargo.toml` 精确固定）。
