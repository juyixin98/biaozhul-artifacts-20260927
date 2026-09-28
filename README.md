# 离线 PCAP IPv4 分片重组后端

一个**完全离线、不发送任何网络报文**的 IPv4 分片重组（IP reassembly）后端。
输入全部来自本地合成夹具或本地 pcap 文件；HTTP 接口默认且仅绑定回环地址。

- 语言/依赖：**Go 1.23 + 标准库（net/http、database/sql）+ SQLite**
- SQLite 驱动：[`modernc.org/sqlite v1.34.5`](https://pkg.go.dev/modernc.org/sqlite)（**纯 Go，无 CGO 运行依赖**）
- 算法标识：`rfc791-rfc815-strict-no-overlap`，版本 `0.1.0`

---

## 1. 目录结构与模块关系

```
cmd/reasm/            CLI 入口：serve / replay / fixture / version
internal/
  config/             配置解析：JSON 文件 + REASM_* 环境变量 + 默认值 + 校验
  netmodel/           网络模型：IPv4 严格解析、RFC1071 校验和、分组键、链路层解封装(ETH/RAW/LOOP)
  reasm/              核心算法：严格无重叠重组、显式超时、状态机、回收（被测核心）
  store/              状态存储抽象 + Memory 实现 + SQLite 实现（组/分片/审计/恢复）
  replay/             经典 pcap 读取器 + 虚拟时间戳回放器（不抓包、不外发）
  fixture/            合成夹具：手工拼 IPv4 首部/分片、确定性 RNG、写 pcap（独立于被测代码）
  oracle/             独立参考实现（位图算法），测试用它与被测核心交叉校验
  api/                回环 HTTP 接口（提交单片 / 回放 pcap / 查询组 / 统计）
  testlog/            测试结构化日志：关联 run_id + 输入身份 + 版本，落 JSONL
  testutil/           测试共享：虚拟时钟、喂片器、全排列、oracle 交叉断言
  version/            版本与算法标识
configs/config.json   默认配置
scripts/verify-offline.sh  一键本地验证（vet + go test -race + 夹具端到端）
testdata/<scenario>/  预生成夹具（capture.pcap + manifest.json）
```

依赖方向（无环）：

```
config, version, netmodel, fixture, oracle, testlog   （底层，互不依赖核心）
store        ─→ netmodel                                   （不依赖 reasm，避免环）
reasm        ─→ netmodel, store
replay       ─→ netmodel, reasm
api          ─→ netmodel, reasm, replay
cmd/reasm    ─→ 以上全部
testutil     ─→ fixture, netmodel, oracle, reasm, store（仅测试）
```

---

## 2. 算法契约与假设

### 2.1 分组键 + 明确超时
- 分组键严格取 RFC 791 四元组：**源地址、目的地址、协议号、16 位标识**。
- **超时附着在“组实例”上**而不是键里：自**首个分片到达**起算 `reassemble_timeout`。
  组终结/回收后，相同键值可被新一轮复用。
- 这是 **IP 层数据报重组，不是 TCP 流重组**：不看端口、不看 TCP 序列号/ACK，
  载荷只按 IP 分片偏移拼接。用例 `TestGroupKeyDistinguishesFlow` 显式固化该区别。

### 2.2 偏移以 8 字节为单位；长度与总量上限
- `Fragment Offset` 为 13 位字段，单位 8 字节；起始字节 = `offset8 * 8`。
- **MF=1 的非末片载荷长度必须是 8 的倍数**，否则其后继片的偏移无法用 13 位字段表达，
  判为 `unaligned_fragment`（单片非法，HTTP 422，**不污染**同一键下已有的组）。
- 任一片终点超过 `max_datagram_bytes`（默认 IPv4 硬上限 65535）→
  `datagram_too_large`（整组拒绝）。

### 2.3 重叠：整组拒绝；完全重复单独识别
- 新区间 `[start,end)` 与任一**已接受**片字节相交且不是“完全重复片” →
  立即整组拒绝：`overlap_group_rejected`。不做 RFC 815 的“择优保留/覆盖”，
  已收字节**绝不输出**，组终结为 `rejected`（HTTP 409）。
- **完全重复片**单独识别：`(起始偏移, 长度, MF)` 三者相同 **且载荷字节全等**。
  计一次幂等重传（`duplicate=true`），不新增块、不改变状态、不触发拒绝。
- 同区间但 MF 或字节不一致 → 视为冲突覆盖，仍按整组拒绝。
- 类别优先级（oracle 与核心一致）：`unaligned / too_large`（单片）→
  `overlap`（字节相交）→ `conflicting_last_fragment`（末点/总长）。

### 2.4 末片先到、缺片不提前输出
- 只有在 **MF=0 已到** 且 `[0,totalLen)` **连续无缺口覆盖** 时才输出重组字节。
- 末片先到只设置 `has_last / total_length`，`covered=0` 时保持 `pending`。
- 末片终点与已宣告总长不一致，或任一片越过已宣告总长 →
  `conflicting_last_fragment`，整组拒绝。

### 2.5 超时与资源回收 / ID 复用
- `pending` 组越过 deadline：终结为 `timed_out`，**彻底删除组行与活动分片**，
  该分组键**立即可以复用**（对齐真实内核对超时缓冲的回收）。超时事实在回放
  报告 `sweep_timed_out` 与测试 JSONL 中留痕。
- `complete / rejected` 组在 `result_ttl` 内保留审计（可查询、可取重组字节），
  期间相同 ID 的新一轮片被判 `group_already_terminal`（HTTP 409）；
  TTL 到期后由 sweep 彻底删除，ID 再次可用。

### 2.6 状态机
```
                       ┌─────────────── complete  (TTL 留存审计 → 彻底回收)
pending ──收齐且连续──► │
   │                   ├─────────────── rejected  (overlap / conflict-last / too-large)
   │                   └─(TTL 到期)────► 彻底删除
   └──越过 deadline──► timed_out ──► 立即彻底删除（键可复用）
```

---

## 3. HTTP 接口（仅回环，服务端不外连）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/healthz` | 版本、算法标识、离线声明 |
| POST | `/api/v1/fragments` | 提交单片（`packet_base64` 裸 IPv4，或手工字段） |
| POST | `/api/v1/replay/pcap` | 上传本地 pcap 字节回放，返回逐帧审计报告 |
| GET | `/api/v1/groups/<规范键>` | 查询组状态/进度/重组字节 |
| DELETE | `/api/v1/groups/<规范键>` | 立即彻底删除组 |
| GET | `/api/v1/stats` | 活动组数、存储组数/分片行数 |

规范键形如：`10.0.0.1->10.0.0.2/proto=17/id=4660`。

**失败绝不折叠成成功**：400（解析类，含具体 `bad_header_checksum` 等）、
422（`unaligned_fragment`）、409（`overlap_group_rejected` /
`conflicting_last_fragment` / `datagram_too_large` /
`group_already_terminal`）、404（`group_not_found`）。

---

## 4. 本地验证

### 4.1 一键验证（推荐）
```bash
./scripts/verify-offline.sh
```
依次执行：`go vet` → `go test ./... -race -count=1`（详细日志同时落
`test-results/go-test-verbose.log`）→ 构建 CLI → 生成并回放 6 类合成夹具。
脚本设置 `GOPROXY=off GOSUMDB=off`，**不联网**。

预期末尾输出：
```
OK  ordered -> complete
OK  permutation -> complete
OK  duplicate -> complete
OK  overlap -> overlap_group_rejected
OK  conflict-last -> conflicting_last_fragment
OK  timeout-reuse -> timed_out once then complete
全部本地验证通过。
```

### 4.2 常用手动命令
```bash
# 全量测试（带竞态检测）
go test ./... -race -count=1

# 只跑核心算法 / 矩阵用例
go test ./internal/reasm -run Permutation -v
go test ./internal/reasm -run Matrix -v

# 生成并回放单场景
go build -o reasm ./cmd/reasm
./reasm fixture -outdir /tmp/f -scenario overlap -size 60
./reasm replay  -pcap /tmp/f/capture.pcap -timeout 500ms -after 2s

# 启动回环 HTTP 服务（SQLite 持久化）
./reasm serve -config configs/config.json
```

### 4.3 如何判断结果
- 单元测试断言**具体结果与失败类别**（不是“接口可调”）；
  `testutil.AssertOracleAgrees` 同时驱动被测核心与**独立 oracle**，
  要求结论类别与重组字节逐字节一致——参考答案不来自被测实现自身。
- 每次测试运行生成 `test-results/<suite>-<run_id>.jsonl`，每行含
  `run_id / case / input_id / version / go / level / message / detail`，
  可把输入身份、计算步骤（STEP）、判定依据（basis）与 PASS/FAIL 关联起来。
- 覆盖矩阵：
  - **全排列**：4 片 24 种到达顺序（含末片先到）全部重组正确；
  - **排列 × 完成前完全重复注入**：数百组合全部幂等且字节正确；
  - **重叠注入 / 冲突末片注入**：多个注入点全部命中正确拒绝类别，并校验资源回收；
  - **超时 × 排列**：每一种排列“缺片超时→彻底回收→同 ID 复用→成功”。

---

## 5. 依赖版本与离线说明

| 组件 | 版本 |
|---|---|
| Go | 1.23（在 go1.23.4 linux/amd64 验证） |
| modernc.org/sqlite | v1.34.5（纯 Go） |
| golang.org/x/sys | v0.22.0（间接） |
| modernc.org/libc | v1.55.3（间接） |

所有第三方依赖均为本地模块缓存中已存在版本，`go.mod`/`go.sum` 已固定；
构建与测试在 `GOPROXY=off` 下完成，不需要生产账号、不需要真实业务数据、
不发起任何网络连接。

## 6. 测试状态（诚实标注）
- 全部包在 `-race` 下通过；`go vet` 无告警；6 类夹具端到端回放符合预期。
- 未覆盖/未实现项：不解析 IPv4 选项语义（按 IHL 整体跳过，不影响重组）；
  不支持 pcapng（仅经典 pcap micro/nano、LE/BE）；未实现 RFC 815 的重叠择优
  （按契约明确采用“整组拒绝”策略）。
