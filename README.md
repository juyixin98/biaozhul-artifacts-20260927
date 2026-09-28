# cidrsvc — 最小无重叠 CIDR 覆盖服务

一个**纯后端**服务：给定「允许地址集合」与「排除地址集合」，计算
`允许 − 排除` 的**最小、无重叠、无遗漏、无额外地址**的 CIDR 覆盖。支持 IPv4 与
IPv6，全程大整数区间运算，**不枚举地址**；每次结果都附带可机器复核的证明。

- 语言：Go（仅标准库 `net/netip`、`math/big`、`net/http`）
- 存储：SQLite（`modernc.org/sqlite`，**纯 Go 驱动，无需 CGO / gcc**）
- 无外部业务账号、无真实业务数据；全部数据为本仓库内合成夹具。

## 1. 模块结构（多模块后端，各司其职）

```
cmd/cidrsvc/main.go          装配与生命周期（启动、优雅关停）
internal/config/             配置解析：JSON + 环境变量覆盖，严格校验
internal/netmodel/           网络模型与核心算法
    prefix.go                CIDR 解析、类型化错误、序数区间（边界策略）
    ranges.go                大整数区间的并 / 差
    cover.go                 区间 -> 最小 CIDR 覆盖（贪心）+ 族转换
    verify.go                独立校验：重叠/遗漏/额外/兄弟可合并
    engine.go                解析->差->覆盖->校验 流水线、步骤追踪
internal/store/              SQLite 状态存储（请求/结果/证据持久化、回放查询）
internal/service/            HTTP 处理器、请求身份关联、结构化日志、恢复中间件
testdata/fixtures/           黄金夹具（人工推导的输入/期望输出/错误类别）
fixtures_test.go             加载夹具做精确断言
configs/config.json          示例配置
examples/curl_examples.sh    服务调用示例（正常 + 异常 + 回放）
docs/ALGORITHM.md            算法与正确性证明（最小性、无兄弟可合并）
```

核心机制由真实模块承担，没有硬编码演示分支。

## 2. 快速开始（复现）

需要 Go 1.23+。依赖版本由 `go.mod` / `go.sum` 锁定；本仓库在**完全离线**
（`GOPROXY=off`）下即可构建与测试——所需模块已在本机模块缓存中。

```bash
make build          # 编译到 bin/cidrsvc（离线，GOPROXY=off）
make test           # 全量测试（含 65536 例 width=4 穷举）
make test-short     # 快速门（跳过最慢的穷举扫描）
make vet            # go vet
make verify         # vet + 全量测试
make run            # 监听 127.0.0.1:8080，SQLite 落 data/
```

如在一台全新机器上首次拉取依赖，可联网执行一次 `NET=1 make build`
（使用默认 `GOPROXY=https://proxy.golang.org`），之后 `go.sum` 不变。

运行后另开终端：

```bash
bash examples/curl_examples.sh
```

## 3. HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/v1/compute` | 计算覆盖；body：`{family, allow[], exclude[], strict?}` |
| GET  | `/v1/requests/{id}` | 按请求身份回放某次计算（成功或失败） |
| GET  | `/v1/requests?family=&status=&limit=&offset=` | 列表/过滤 |
| GET  | `/v1/stats` | 按状态聚合计数 |
| GET  | `/healthz` | 健康检查 |

**请求身份关联**：客户端可用 `X-Request-Id` 显式指定关联 ID（不指定则生成
`req_<hex>`），响应头与响应体都回带；`X-Client-Ref` 可携带工单号等外部引用。
日志、持久化记录、回放均以该 ID 串联。

成功响应包含：

- `prefixes`：最小 CIDR 列表；
- `proof`：`target_address_count` / `cover_address_count`（必须相等）、
  `exactly_equivalent`、`overlaps`、`mergeable_siblings`；
- `steps`：`resolve_family → parse_allow → parse_exclude → union_and_subtract →
  greedy_cover → convert_and_format → independent_verification`，每步带耗时与
  处理位置（`location`）；
- `warnings`：非致命的不确定/归一化（如主机位被掩码）单列；
- `version`：构建版本。

失败响应（`status:"error"`）单独给出 `error_code`（失败类别）、`error`（原因）、
`location`（处理位置）、`request_id`。类别示例：

| error_code | 含义 | HTTP |
| --- | --- | --- |
| `malformed_cidr` | 无法解析 / 缺少斜杠 / 掩码非数字 | 400 |
| `prefix_length_too_long` | 前缀长度超过地址宽度（如 v4 `/33`） | 400 |
| `host_bits_present` | strict 模式下地址含主机位 | 400 |
| `family_mismatch` | 同一请求混用 IPv4/IPv6 | 400 |
| `too_many_prefixes` / `invalid_json` | 输入约束 | 400 |
| `verification_failure` | 独立后置校验未通过（不确定结论） | 500 |

## 4. 边界策略（固定）

CIDR 区间**同时包含网络地址与定向广播地址**：`/31`=2 个地址、`/32`=1 个地址。
完整证明见 `docs/ALGORITHM.md`。

## 5. 可复核性（参考答案独立于被测实现）

测试不使用被测核心自身生成答案，而是三层交叉验证：

1. **位枚举真值**：width=3 全部 256 种、width=4 全部 **65536** 种目标子集逐一
   枚举，用朴素整除判定成员资格；
2. **Trie 标注 oracle**：与生产贪心不同的独立算法，并在小位宽与位真值逐位比对；
3. **被测算法**输出须同时等于前两者并通过结构校验。

另含：width=5「允许前缀×排除前缀」全 3969 配对、width 4..10 随机差集（含重复/
相邻/重叠）、真实 IPv4 具体字面值、IPv6 极值（`::/0`、`::/128`、
`ffff...ffff/128`）、全空间与空集。校验器的每一类失败（重叠、遗漏、额外、
越界、错配、重复、非法掩码、可合并兄弟）都有**注入缺陷后断言具体类别**的测试。

黄金夹具 `testdata/fixtures/golden_cases.json` 给出人工推导的期望前缀与精确地址
计数，由 `fixtures_test.go` 精确比对。

```bash
go test ./... -count=1 -v   # 查看逐子用例结果
```

## 6. 配置

`configs/config.json`，可用环境变量 `CIDRSVC_HTTP_LISTEN`、`CIDRSVC_DB_PATH`、
`CIDRSVC_LOG_PATH`、`CIDRSVC_MAX_INPUT_PREFIXES`、`CIDRSVC_STRICT_CIDR`、
`CIDRSVC_SHUTDOWN_TIMEOUT` 覆盖。配置解析拒绝未知字段（`DisallowUnknownFields`），
拼写错误会直接启动失败而非静默回退。

## 7. 日志与回放

日志为每行一个 JSON 对象（stderr 或文件），字段含 `request_id`、`client_ref`、
`http_status`、`duration_ms`、`error_code`、`error`、`location`、`version` 等，
失败原因与（独立校验产生的）不确定结论与正常访问分离（`level=error`，
独立 `message`）。所有成功与失败请求都落 SQLite，可通过 `/v1/requests/{id}` 回放，
失败记录包含错误类别与原因，但不携带伪造的成功结果。

## 8. 依赖锁定

- Go：1.23（`go.mod`）
- SQLite：`modernc.org/sqlite v1.34.4`（纯 Go，无 CGO），间接依赖与哈希全部记录
  在 `go.sum`（已校验，离线可复现）。
