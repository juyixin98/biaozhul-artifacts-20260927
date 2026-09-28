# 复现文档

环境：Linux x86_64，Rust 1.98.1（2026-09-01），仅使用 crates.io 依赖，
版本由仓库根目录 `Cargo.lock` 锁定。无需任何外部账号或网络服务，
依赖首次构建时由 cargo 拉取（本机使用清华 sparse 镜像，配置在用户级
`~/.cargo/config.toml`，与项目无关）。

## 1. 构建与全量测试

```bash
cargo build --release
cargo test --release
```

已保留的真实输出：`docs/run_logs/cargo_test_release.log`。
结果：**23 个测试全部通过**

| 测试二进制 | 用例数 | 内容 |
|---|---|---|
| 库内单测 (`src/**`) | 8 | 位向量 rank、压缩保序、配置解析、FNV/损坏检测 |
| `tests/wm_reference.rs` | 7 | 随机差分（排序/线性扫描 oracle）、全同值、有符号极值、手算答案、错误分类 |
| `tests/persistence.rs` | 4 | 保存/加载一致、字节级确定性、损坏/截断/版本、名称与缺失分类 |
| `tests/api_tests.rs` | 4 | HTTP 全生命周期、手算查询值、失败状态码与 kind、请求身份 |

静态检查：

```bash
cargo clippy --all-targets   # 无 warning（输出见 cargo_clippy.log）
```

## 2. 启动服务（正常路径）

```bash
./target/release/wavelet-matrix-service --config config/default.toml
# 另一个终端：
bash examples/query_examples.sh
```

脚本逐个演示：health、建索引、列举、第 k 小、计数、前驱、后继，
以及两个失败样例。配置项见 `config/default.toml`
（`listen` / `data_dir` / `log_level`）。

## 3. 异常路径手工验证

```bash
B=http://127.0.0.1:18080
# k 越界（区间长度 8，k=8）
curl -s -w '\nHTTP %{http_code}\n' -X POST $B/v1/indexes/demo/queries \
  -H 'Content-Type: application/json' \
  -d '{"op":"kth_smallest","l":0,"r":8,"k":8}'
# -> HTTP 400, error.kind = "k_out_of_bounds"

# 空区间 [3,3)
curl -s -w '\nHTTP %{http_code}\n' -X POST $B/v1/indexes/demo/queries \
  -H 'Content-Type: application/json' \
  -d '{"op":"count_lt","l":3,"r":3,"bound":0}'
# -> HTTP 400, error.kind = "empty_range"

# 不存在的索引
curl -s -w '\nHTTP %{http_code}\n' -X POST $B/v1/indexes/ghost/queries \
  -H 'Content-Type: application/json' \
  -d '{"op":"count_lt","l":0,"r":1,"bound":0}'
# -> HTTP 404, error.kind = "index_not_found"
```

完整真实转录（含全部响应与状态码）保存在
`docs/run_logs/http_smoke.log`；服务端关联日志保存在
`docs/run_logs/server.log`（每条带 `request_id`、阶段与
`format_version`，失败行带 `error_kind`/`status`）。

## 4. 构建—保存—加载一致性

1. 启动服务，建索引后 `data/<name>.wmx` 落盘（建索引响应含
   `persisted_path` / `persisted_bytes`）。
2. 重启服务，启动日志出现
   `loaded index from data directory ... format_version=1` 与
   `restored N persisted index(es)`；不重新提交数据即可继续查询。
3. 自动化层面：`tests/persistence.rs` 还断言
   - 加载后的索引与原索引 `PartialEq` 相等、所有窗口查询答案一致；
   - 重新编码产物与磁盘字节逐字节相同（格式确定性）；
   - 截断、翻转载荷字节、坏 magic、未知版本分别报
     `corrupt_format` / `unsupported_version`。

## 5. 夹具

`fixtures/sample_arrays.json` 为最小合成数据：
混合重复值小数组（附手算答案）、全同值、纯负值、
`i64::MIN/MAX` 极值数组、升序数组。测试与示例中的期望值全部可由
该文件手工复核，不由被测代码生成。
