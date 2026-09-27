# prereg2d — 离线预注册坐标的二维点增量 / 矩形和查询后端

Rust + Axum + 本地文件系统实现。坐标必须**先注册**后更新；每次批更新
**原子发布**一个不可变版本；矩形求和为**两边界包含（闭区间）**语义；
坐标表可以重建，**所有历史版本仍可查询**。

## 工程分层

| 层 | 路径 | 职责 |
|---|---|---|
| 数据格式 | `src/model.rs` | 坐标/权值/矩形/版本类型与边界语义 |
| 错误分类 | `src/errors.rs` | 稳定错误码、HTTP 状态映射、请求关联 |
| 索引内核 | `src/index/compress.rs` | 固定升序、去重的坐标压缩 |
| | `src/index/grid.rs` | 稠密 `i64` 点权网格（带溢出检查） |
| | `src/index/fenwick.rs` | 二维 Fenwick（内部 `i128`）+ 判定步骤明细 |
| 版本快照 | `src/version.rs` | 每版本独立表/网格/索引，不可变 |
| 持久化适配 | `src/store/mod.rs` | 追加日志（逐行校验和 + fsync）+ `CURRENT` |
| 领域服务 | `src/service.rs` | 校验、拒绝策略、原子发布、重建携值、重放 |
| 验证接口 | `src/api.rs` | Axum JSON HTTP API、请求 ID、错误归类 |
| 配置层 | `src/config.rs` | 默认值 < TOML 文件 < 环境变量 |

详细语义见 [`docs/SEMANTICS.md`](docs/SEMANTICS.md)，架构与崩溃恢复见
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)。

## 固定版本依赖

`rust-toolchain.toml` 固定工具链 `1.98.1`；`Cargo.toml` 所有依赖用
`"=x.y.z"` 精确锁定，`Cargo.lock` 一并交付，保证可重复构建。

## 快速开始

```bash
cargo run --release --bin prereg2d-server -- config.example.toml
# 或仅用环境变量
PREREG2D_DATA_DIR=./data PREREG2D_BIND=127.0.0.1:8080 \
  cargo run --release --bin prereg2d-server
```

### 接口一览

| 方法/路径 | 说明 |
|---|---|
| `POST /admin/register` | 首次注册坐标表 `{xs:[i64], ys:[i64]}` → v1 |
| `POST /admin/rebuild`  | 重建坐标表（存活点携值）→ 新版本 |
| `POST /batches`        | 原子点增量批 `{updates:[{x,y,delta}]}` → 新版本 |
| `GET /query?version=&x_lo=&x_hi=&y_lo=&y_hi=` | 闭区间矩形和（`version` 省略为 head） |
| `GET /versions` / `GET /versions/:v` | 版本清单 / 单版本元数据 |
| `GET /health` | 存活与 head 版本 |

边界字段为 **i128**（可表达超出 i64 的矩形边界）；点坐标与增量为 **i64**。

```bash
curl -s localhost:8080/admin/register -H 'content-type: application/json' \
  -d '{"xs":[-5,0,7],"ys":[-3,2,100]}'
curl -s localhost:8080/batches -H 'content-type: application/json' \
  -d '{"updates":[{"x":-5,"y":-3,"delta":3},{"x":0,"y":2,"delta":-5}]}'
curl -s 'localhost:8080/query?version=2&x_lo=-5&x_hi=0&y_lo=-3&y_hi=2'
# {"sum":-2,"version":2,"explain":{...四项前缀和与cutoff...},...}
```

成功统一含 `"ok":true`；失败统一含 `"ok":false, "error_code", "error",
"request_id"`，绝不把异常/未知状态返回成成功。

### 错误码

| error_code | HTTP | 触发 |
|---|---|---|
| `not_initialized` | 412 | 未注册就更新/重建/查询 |
| `unregistered_coordinate` | 422 | 更新了未注册坐标（拒绝，不错位插入） |
| `duplicate_in_batch` | 422 | 同一批内重复坐标 |
| `empty_batch` | 422 | 空批次 |
| `overflow` | 422 | 点累计和越过 i64 |
| `empty_axis` | 422 | 注册/重建时空轴 |
| `bad_request` | 422 | JSON/查询参数无法解析 |
| `payload_too_large` | 413 | 超请求体上限 |
| `unknown_version` | 404 | 版本号不存在 |
| `not_found` | 404 | 未知路由 |
| `persistence_error` | 500 | 日志损坏、校验和不符、CURRENT 不一致 |

## 测试与可复现性

- 独立测试在 `tests/`：内核（4）、服务（3）、原子性（2）、持久化（3）、
  HTTP（3）、配置（1），共 **16** 个。
- 期望值来源**独立于被测内核**：
  - `fixtures/handcalc.json`：手算值（重复坐标、负权、空矩形、极端坐标、
    重建、旧版本）；
  - `tests/common/mod.rs::oracle`：仅用 std 的 `HashMap` **全扫描**参考实现，
    随机轨迹下与被测系统逐版本、逐矩形比对；
  - `tests/kernel.rs` 内部另有独立全扫描。
- 随机输入使用固定种子 LCG（`Lcg::seeded(...)`），跨机器可复现。
- 每个用例日志带运行身份（时间戳+pid+序号）、输入、版本、进度、计算步骤
  和判定依据；失败时整体转储。HTTP 失败回显 `x-request-id`，与服务端日志
  关联。

一键验证（格式、clippy、全部测试、真实起服务、HTTP 冒烟、重启持久化复核）：

```bash
./scripts/verify.sh                 # SKIP_SMOKE=1 可跳过起服务阶段
python3 scripts/smoke_http.py <base_url> <run_id>   # 仅跑 HTTP 冒烟
```

未在本环境执行的检查（例如断电级耐久性、NFS 语义、压测）单列在
[`docs/NOT-RUN-CHECKS.md`](docs/NOT-RUN-CHECKS.md)，**不视为已通过**。
