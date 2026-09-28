# 复现指南

环境：Linux x86_64，Rust 1.98.1（MSRV 1.80）。依赖已在 `Cargo.lock` 锁定，
本机 `~/.cargo` 已缓存全部 crate，全部命令支持 `--offline`。

## 1. 构建

```bash
cargo build --release --offline
# 产物：
#   target/release/lz77b          命令行
#   target/release/lz77b-server   HTTP 服务
```

## 2. 最小夹具

夹具由独立生成器产出，生成器用本地一次性 LEB128/CRC/头部代码手工组装块，
**不经过被测编码器**，并具有确定性（重复生成 md5 不变）：

```bash
cargo run --offline --example gen_fixtures
find fixtures -type f | sort
# fixtures/blocks/block-0000000{0,1,2}.lzb   手工 3 块链（独立+2 依赖）
# fixtures/blocks/expected.json              三块钉死的预期明文
# fixtures/sample1.bin                       4585 字节，强制重叠/跨窗口
# fixtures/sample1.meta.json                 长度 + FNV-1a64 钉值
```

手工链按肉眼可核：块0 为 `abcdefghi` + 13 个 `j`（1 字面 + 12 重叠复制）+
`tail-0`；块1 为 `xyz` + 从字典取的 `tail-0` + 距离1 重叠出的 `0000` +
`one`；块2 以距离2 重叠出 `ABABABABAB` 再跨窗口取 `tail-0`。

## 3. 真实运行（正常路径）

```bash
# 单块压缩并用两个解压器互验
./target/release/lz77b compress fixtures/sample1.bin /tmp/s.lzb
# compressed 4585 -> 155 bytes, 14 match tokens
./target/release/lz77b verify /tmp/s.lzb
# OK: core and independent reference agree on 4585 bytes (frame=0, index=0)

# 不同分块模式恢复相同字节
for c in 16 100 1024 4096; do
  ./target/release/lz77b chain fixtures/sample1.bin /tmp/c$c $c
  ./target/release/lz77b unchain /tmp/c$c /tmp/o$c.bin
  cmp fixtures/sample1.bin /tmp/o$c.bin && echo "chunk=$c OK"
done
```

已留存结果见 `docs/evidence/cli-smoke.txt`。

## 4. 真实运行（HTTP）

```bash
./target/release/lz77b-server --listen 127.0.0.1:18080 --store /tmp/store
bash examples/http-calls.sh           # 另一终端：正常 + 全部异常路径
```

已留存的请求/响应与服务端日志：

- `docs/evidence/http-smoke.txt` — healthz、建流、两块编码、整链解码、
  400/409/413、cross_check；
- `docs/evidence/server-smoke.log` — 带 run id 的服务端结构化日志。

错误类别 → HTTP 状态：input=400，state=409，resource=413，compute=500。

## 5. 异常与对抗输入

| 场景 | 构造方式 | 期望 |
|---|---|---|
| 自重叠长匹配 | 7 万字节同一字符 | 距离1 匹配，逐字节还原 |
| 跨窗口 | 短语相隔 >4096 字节复现 | 过期历史不被引用 |
| 错误距离 | 字面1字节后 `distance=100` | `bad_distance`（input） |
| 缺前块 | 新会话直接喂 index=1 依赖块 | `index_gap`（state） |
| 摘要错配 | 改头部 prev_digest 而载荷 CRC 仍正确 | `digest_mismatch`（state） |
| 载荷损坏 | 翻转载荷位 | `crc_mismatch`（input） |
| 输出炸弹 | 1 字节载荷声明 4 GiB | 头部拒绝 `output_cap_exceeded`，RSS +0 KiB |
| 倍率炸弹 | 小载荷声明近 1 MiB | `expansion_cap_exceeded`（与上者可区分） |
| 运行时超长 | 声明4字节却发距离1×200匹配 | 第5字节前 `length_mismatch` |
| 存储总量 | 32 字节容量写 40 字节块 | `total_cap_exceeded`，无 .lzb 落盘 |
| 整链解压聚合 | 两个 ~40 字节高比率块各解压 512 KiB，流上限 700 KiB | `stream_output_cap_exceeded`（resource） |
| 非规范 base64 | `AAAAZg=A`（填充越位）、`ZR==`（尾随非零位） | `bad_string`（input），HTTP 400 |
| 路径穿越 | stream id `../etc/passwd`、`a/b`、`..` | `bad_string`（input） |

## 6. 测试（证据）

```bash
rm -rf test-results
cargo test --offline 2>&1 | tee docs/evidence/test-run.txt
# 26 个单元测试 + 29 个证据用例（55 个），debug 与 release 全部 ok
```

每个用例产出 `test-results/runs/<run-id>.log`，文件头记录 rustc 版本与固定
常量，事件行包含序号、UTC 时间、STATE/PASS/FAIL、期望值、实际值与判定理由；
`test-results/summary.jsonl` 每用例一行。断言失败先写 FAIL 再 panic，因此崩溃
现场也可凭 run id 重放。本次已留存结果见 `test-results/` 与
`docs/evidence/test-run.txt`。

例：

```
$ cat test-results/runs/04-output-cap-*.log
# constants: WINDOW=4096 MIN_MATCH=3 MAX_MATCH=65538 MAX_PAYLOAD=65536 ...
001 STATE :: rss-before-kb = 3072
002 STATE :: rss-after-kb = 3072
003 STATE :: rss-delta-kb = 0
004 RESULT :: verdict=PASS events=3 elapsed_ms=0
```

## 7. 静态检查

```bash
cargo clippy --offline --all-targets     # 无警告
```

## 8. 为什么参考验证是可信的

- `src/reference/` 不 `use` 任何 `core` 项：自带字节游标、LEB128、CRC 多项式、
  头解析、逐字节重叠复制与三分类错误；
- 三个手工夹具块由 `examples/gen_fixtures.rs` 用第三套局部代码组装，明文在
  `expected.json` 钉死，可肉眼核对；
- 证据测试要求：同一分块下 core 与 reference 字节相等，且二者都等于钉死
  明文；错误输入两者都必须拒绝（reference 为无状态解析器，“错误但等长字典”
  会解出垃圾，该事实本身也被断言，用以说明摘要绑定为何必要）。

## 9. 对抗式复审修复记录

实现完成后做了两轮独立代码复审（编解码内核、存储/HTTP 契约），确认并修复：

1. **参考解压器对畸形字面量长度 panic**：`u64::MAX` 字面量长度导致
   `pos+len` 回绕并切片越界（debug/release 均崩），而 core 返回类型化错误。
   改为减法边界 + `usize` 适配检查；回归用例 `01-giant-literal`，release 下
   独立 PoC 确认不再 panic。
2. **解码器接受超出窗口的 distance**：即使历史中存在该字节，distance>4096
   现按固定格式判 `bad_distance`，两个解压器一致；回归 `01-distance-window-spec`。
3. **块序号 u32 溢出**：`checked_add` 返回 `index_gap` 而非回绕/panic。
4. **整链解压无聚合上限（内存放大 DoS）**：新增流解压上限（默认 16 MiB，
   每块按声明长度在解压前预算），回归 `04-stream-output-cap`。
5. **base64 非规范填充/尾随位**：重写为严格 RFC4648，拒绝越位 `=` 与非零
   尾随位，HTTP 层与单测双重回归。
6. **全局会话锁跨越磁盘回放**：回放移出锁外，文件 I/O 与编解码 CPU 全部经
   `spawn_blocking`，不再阻塞 async 工作线程与其他流。
7. 原子写 rename 失败时清理残留 `.tmp`；encode 路径自校验失败时从磁盘重建
   会话缓存，杜绝内存领先于持久状态。
8. 本地符号链接加固：重扫只索引真实普通文件（`symlink_metadata`），块写入
   用 `O_EXCL|O_NOFOLLOW` 拒绝穿过符号链接写出存储根（回归 `05-symlinks`）。
9. `not_found` 在 HTTP 层按 REST 映射为 404（响应体仍保留 category=state），
   其余 state 冲突保持 409（回归用例断言两者）。
