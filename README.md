# natlab — 本地可回放的有状态 NAT 模型

`natlab` 把一个**有状态、端点相关（symmetric）的 NAT** 建模成纯软件：输入是报文
五元组元数据（JSON），输出是 NAT 映射后的五元组、连接状态，以及对非法/冲突/耗尽
报文的**分类拒绝原因**。它不触碰宿主机网络栈，所有 IP 都是合成的（`203.0.113.0/24`、
`10/8` 等保留段），数据全部落在本地 SQLite 文件或内存中。

- 语言/依赖：Go（标准库 `net/netip`、`net/http`、`encoding/json`）+ SQLite（纯 Go
  驱动 `modernc.org/sqlite`，**无需 cgo**）。
- 时间源：报文自带时间戳，经每个 run 的**单调高水位时钟**处理；时间回拨只标记
  `clock_rewind`，绝不复活已过期连接。
- 可回放：每个判决写一行事件日志（run id、序号、观测/生效时间、关键中间状态、判定
  理由），夹具（fixture）是手写的独立预言。

---

## 1. 五分钟上手

前置：Go 1.23+（无需联网构建，依赖已在模块缓存；也可联网 `go mod tidy`）。

```bash
make            # 构建 bin/natlab
make test       # 全部单元 + 集成测试
make replay FIXTURE=testdata/fixtures/02_bidirectional_flow.json
```

启动本地 HTTP 服务：

```bash
make serve      # 等价于 bin/natlab serve -config configs/natlab.json
```

另开终端，喂第一个出站报文：

```bash
curl -s -X POST http://127.0.0.1:8080/runs/demo/packets \
  -H 'Content-Type: application/json' \
  -d '{"ts":"2026-01-01T00:00:00Z","src_ip":"10.0.0.10","src_port":5001,
       "dst_ip":"198.51.100.1","dst_port":53,"protocol":"UDP","direction":"outbound"}'
```

返回（源地址被改写为公网地址 + 分配的外部端口）：

```json
{"run_id":"demo","accepted":true,"mapped_port":40000,"state":"open",
 "translated":{"src_ip":"203.0.113.1","src_port":40000,
               "dst_ip":"198.51.100.1","dst_port":53,"protocol":"UDP"}}
```

查看判决日志与当前映射：

```bash
curl -s http://127.0.0.1:8080/runs/demo/events
curl -s "http://127.0.0.1:8080/runs/demo/mappings?active=true"
```

停止服务：`Ctrl-C`（或 `pkill -f 'natlab serve'`）。

---

## 2. 命令行

```
natlab serve   -config configs/natlab.json
natlab replay  -config <cfg> -fixture <fixture.json> [-run-id ID] [-mem]
```

- `serve`：启动本地 HTTP 回放接口（默认 `127.0.0.1:8080`）。
- `replay`：离线回放一个夹具，逐包打印判决与是否匹配预言，末行输出
  `passed=true/false`；**断言失败时退出码为 2**，可直接进 CI。
  - 不带 `-mem`：使用临时内存 SQLite（真实 SQL，不落盘）。
  - `-mem`：使用进程内假存储（更快，同构语义）。集成测试两种后端都会跑。

---

## 3. 行为模型（精确约定）

| 主题 | 约定 |
|---|---|
| 映射类型 | 端点相关：键为完整私有五元组（协议+内网源端点+远端端点）。同一内网端点访问不同远端 → 不同映射/端口。 |
| 入站过滤 | 入站包的源必须等于该映射记录的远端端点，否则 `ENDPOINT_FILTERED`。 |
| 端口分配 | 从端口池取**当前最低空闲端口**；活跃映射的端口绝不二次复用；过期/RST/完整 FIN 关闭后归还，可被新流复用。 |
| 端口耗尽 | 池满时首包被拒：类别 `resource_exhausted`，码 `PORT_EXHAUSTED`。 |
| UDP | 单一 `open` 态，TTL（默认 180s），匹配端点的任一包刷新 TTL。 |
| TCP | `syn_sent → syn_ack_rcvd → established → fin_wait → closed`；每态独立 TTL（`syn_sent` 30s、瞬态 60s、`established` 7440s）。裸 ACK/乱序标志 → `TCP_BAD_STATE`；未知流上的非 SYN 首包 → `TCP_NON_SYN_OUTBOUND`；RST（任一侧）或完整 FIN 交换立即关闭并释放端口。 |
| 返回包 | 必须匹配连接状态：端口从未拥有→`INBOUND_NO_MAPPING`；映射已过期/关闭→`LATE_RETURN_EXPIRED`。 |
| 分片 | 仅接受**已重组输入**。携带 `fragment`（offset>0 或 MF）→ `FRAGMENT_NOT_REASSEMBLED`。 |
| 时钟 | 每 run 单调高水位。过期判定用生效时钟 `now=max(历史, ts)`；旧时间戳只置 `clock_rewind`，不延长任何寿命。过期在每包处理前以 `expires_at <= now` 清扫。 |
| TCP/UDP 超时 | 完全分开配置、分开计时。 |

### 失败分类（四类，可区分）

- `invalid_input`：`INVALID_TS` `BAD_DIRECTION` `UNSUPPORTED_PROTOCOL` `BAD_FLAG`
  `UDP_FLAGS_NOT_ALLOWED` `BAD_SRC_IP` `BAD_DST_IP` `BAD_SRC_PORT` `BAD_DST_PORT`
  `FRAGMENT_NOT_REASSEMBLED` `SRC_NOT_PRIVATE` `INBOUND_DST_MISMATCH`
- `state_conflict`：`TCP_NON_SYN_OUTBOUND` `TCP_BAD_STATE` `ENDPOINT_FILTERED`
  `INBOUND_NO_MAPPING` `LATE_RETURN_EXPIRED`
- `resource_exhausted`：`PORT_EXHAUSTED`
- `compute_failure`：存储/内部故障（`STORE_ERROR`），HTTP 边界映射为 **500**；前三类
  是模型判决，HTTP 一律返回 **200 + 判决体**。

---

## 4. 工程结构（模块边界与契约）

```
cmd/natlab/              程序入口：serve / replay 两个子命令 + HTTP 启动/访问日志
internal/
  model/                 数据与错误契约：FiveTuple/Packet/Mapping/Decision/Event、
                         四类 Category 与稳定 Code 常量（所有层共用，禁止临时造类型）
  config/                配置解析/校验（公网IP、内网CIDR、端口池、TCP/UDP 独立超时）
  nat/                   网络模型核心：引擎、单调时钟、端口池、TCP 状态机、StateStore 接口
  store/                 StateStore 的 SQLite 实现（部分唯一索引、过期清扫、事件落盘）
  memstore/              StateStore 的内存实现（测试 / -mem），不含任何 NAT 判定逻辑
  replay/                回放接口：夹具加载、顺序组/并发组调度、与手写预言比对、不变量
  server/                标准库 HTTP 边界（评估单包/批量、查事件、查映射、健康检查）
  testutil/              测试运行编号与可重放日志（testdata/runs/<组件>/run-RUN-NNNN.log）
configs/natlab.json      启动配置样例
testdata/fixtures/*.json 手写预言夹具（编号 01..09，见下）
```

数据流：`Packet(JSON) → nat.Engine（时钟/清扫/状态机/端口池）→ Decision
（+Mapping/Translated）→ StateStore 落盘 + Event 日志 → HTTP/CLI 输出`。错误在模块
间以 `model.Category/Code`（模型判决）与 `*nat.ComputeError`（计算失败）两种形式传递。

---

## 5. 夹具（独立预言，覆盖你要求的全部验证点）

期望是**人工推导**的（接受/拒绝、类别、错误码、映射端口、状态），不由被测核心生成。

| 夹具 | 验证点 |
|---|---|
| `01_port_exhaustion` | 3 端口池：前三包各得唯一端口，第四包 `PORT_EXHAUSTED`；一一对应 |
| `02_bidirectional_flow` | 完整 TCP 握手 + 双向数据；伪装对端 `ENDPOINT_FILTERED`；FIN 关闭后端口复用 |
| `03_reuse_after_timeout` | UDP 超时清扫、`LATE_RETURN_EXPIRED`、最低空闲端口复用、复用后正常返回 |
| `04_late_returns` | 未拥有端口 / 已过期 / 入站目的 IP 不符 三类迟到返回 |
| `05_concurrent_first_packets` | 20 个首包**同组并发**同时间戳：恰得 20 个不同端口，无重复、无错配 |
| `06_input_validation` | 各类 `invalid_input` 错误码，且全程不产生映射 |
| `07_clock_rewind` | 旧时间戳不复活：`clock_rewind` 置位、生效时钟不回退、过期端口已被新主占用 |
| `08_tcp_state_and_rst` | 非 SYN 首包拒绝；乱序 ACK `TCP_BAD_STATE`；RST 关闭释放；关闭后到达=迟到 |
| `09_udp_endpoint_dependence` | 同内网端点访问两远端得两映射；第三对端打到同一端口仍被过滤 |

夹具格式见 `testdata/fixtures/` 与 `docs/api.md`。同 `group` 的包并发执行，不同组按组号
顺序执行（所以顺序场景里组号=时间顺序）。

---

## 6. 测试

- 独立单元测试：时钟、端口池（耗尽/复用/重建 Burn）、TCP 双向状态机、标志解析、
  配置校验、SQLite（唯一约束/边界清扫/事件往返/时钟持久化/关闭后端口可重用）、
  计算失败可区分性。
- 集成测试：9 个夹具分别跑 **SQLite 与内存两后端**；断言具体端口、状态、类别与错误码，
  外加脚本级不变量（接受数、错误码计数、端口多重集、最终活跃端口唯一）。
- 并发：`-race` 下运行；专用并发首包测试 + 引擎串行锁。

每次测试在 `testdata/runs/<组件>/run-RUN-NNNN.log` 留下可重放日志：运行编号、逐步关键
中间状态（swept 数、active 数、clock_rewind、分配端口）和判定理由。

### 真实测试命令与输出结论

```
$ go test -race ./...
ok  	natlab/internal/config
ok  	natlab/internal/nat
ok  	natlab/internal/replay
ok  	natlab/internal/server
ok  	natlab/internal/store
```
结论：**5 个包全部通过，竞态检测器无告警。**

9 个夹具 × 2 后端（`natlab replay ...` 与 `... -mem`）末行均为
`passed=true failures=0`；故意写错的预言夹具退出码为 `2` 并打印
`FAIL: packet[0] ... mapped_port: want 49999 got 40000`。

HTTP 端到端（真实 curl）验证了：出站映射 40000、匹配返回被转发并改写到内网端点、
伪装对端 `ENDPOINT_FILTERED`、池满 `PORT_EXHAUSTED`、分片 `FRAGMENT_NOT_REASSEMBLED`；
重启进程复用同一 SQLite 文件后，映射仍在且返回包继续匹配（状态持久化）。

> 这些命令均只使用 `127.0.0.1` 与合成保留地址，不创建套接字之外的系统网络变更，也不
> 配置任何真实 NAT/iptables。

更多 HTTP 字段见 [`docs/api.md`](docs/api.md)，设计取舍见 [`docs/architecture.md`](docs/architecture.md)。
