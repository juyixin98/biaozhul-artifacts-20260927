# Aho-Corasick 流式匹配后端（纯后端，本地可验证）

一个 **FastAPI + SQLite** 的本地服务，对**字节流**做 Aho-Corasick 多模式匹配，支持：

- **模式集合版本化**：每次编译得到不可变自动机，元数据与模式持久化在 SQLite；
- **流式 + 原始字节偏移**：任意分块，命中位置是相对整个逻辑流的原始字节偏移，UTF-8 多字节字符跨块也能正确续接；
- **重叠命中 / 后缀模式不漏报**：失败指针（failure link）与输出链（output/dictionary link）完整；
- **版本切换只能在显式边界**：`reset` 是唯一换版本路径，重绕到根节点并推进 `epoch`，绝不混用不同自动机的节点；
- **单点大量命中可恢复分页**：命中按规范顺序落库，HMAC 签名的不透明游标 keyset 分页；
- **诊断可复核**：每个请求带请求标识与「接受 / 拒绝 / 无法判定」决策记录，只存长度/偏移/节点，**不存模式与载荷内容**。

无任何生产账号或外部业务依赖；所有测试数据为本地合成夹具。

---

## 1. 目录与模块职责

```
app/
  errors.py            领域错误分类（稳定 code、HTTP 状态、accept/reject/inconclusive）
  spec.py              文本规范：编码(utf-8/ascii/latin-1/binary)、ASCII 折叠、
                       规范化 base64 传输、增量 UTF-8 解码器（跨块字符边界）
  automaton.py         核心 AC：trie 构建、BFS 失败指针、输出链、字节驱动 feed()
  matcher.py           流式状态机：绑定某个版本的不可变自动机 + 节点/偏移/epoch
  pagination.py        不透明可恢复游标（scan+epoch+after_seq，HMAC-SHA256）
  config.py            环境变量配置（AC_ 前缀，安全本地默认值）
  diagnostics.py       请求/决策记录器与脱敏规则（只存长度、计数、偏移、节点）
  storage/
    db.py                 SQLite 连接、WAL、schema DDL、写锁
    version_repo.py       版本与模式表
    scan_repo.py          扫描生命周期、命中表、keyset 分页
    diag_repo.py          诊断事件表
  services/
    versions.py       “原始 base64 → 校验 → 规范化 → 编译 Automaton → 落库”
    scans.py          打开/投喂/分页/显式 reset/关闭；热状态与持久化一致性
  api/
    schemas.py        Pydantic 请求/响应模型
    deps.py           容器装配与依赖注入
    routes_versions.py    POST/GET /versions…
    routes_scans.py       /scans…（chunks、hits、reset、close）
    routes_diagnostics.py /diagnostics…
  main.py             应用工厂 + 诊断中间件 + 领域错误处理
tests/
  oracle.py           ★ 独立朴素参考实现（逐模式 bytes.find，重叠+1）
  conftest.py         隔离的临时 DB / app / TestClient 夹具
  helpers.py          base64、建版本、开扫描、投喂、自动翻页
  test_automaton.py       失败指针/输出链、后缀、重叠、二进制的具体断言
  test_streaming.py       跨块、UTF-8 字节偏移、空块、编码拒绝
  test_versioning_paging.py 版本守卫、reset/epoch、游标失效、大扇出分页
  test_api_failures.py    各类拒绝的 HTTP 状态 + 稳定错误码 + decision
  test_diagnostics.py     请求标识、决策原因、脱敏（敏感串不得落库）
  test_e2e_chunking.py    HTTP 端到端：不同分块多重集相同（对照 oracle）
  test_persistence.py     模拟进程重启后从 SQLite 续接跨块匹配
  test_spec_pagination.py base64/编码/折叠/游标篡改单测
  test_fuzz.py            12 组确定性随机：模式集 × 二元/四元/256 元字母 × 分块
scripts/
  smoke.py            端到端冒烟（进程内 或 --base-url 打真实服务）
requirements.txt      锁定版本
```

设计上不是单文件脚本：**规范、算法索引、版本存储、查询、诊断**分别有真实职责并独立测试。

---

## 2. 算法假设（请据此复核）

1. **匹配单位是字节，不是 Unicode 码点。** 命中 `start/end` 一律为原始字节偏移（end 为排他）。模式与数据以规范 base64 传输，因此二进制模式（含 `\x00`）是一等公民。
2. **文本模式仅做字节级变换。** 支持 `ascii_casefold`，因为 ASCII 大小写折叠是逐字节且不改变长度的；**不支持**完整 Unicode casefold（会改变字节长度，无法保持字节偏移），这是有意拒绝而非近似。
3. **失败指针与输出链（教科书式构造）。** 到达某个节点后，沿 output link 枚举所有在当前位置结束的后缀模式，因此**后缀模式不会漏报**（含多级链，如 `zabcd/abcd/bcd/cd/d`）。
4. **命中规范顺序（分页契约）：** `(end ASC, start ASC, pattern_id ASC)`。同一结束位置最长模式在前、后缀随后；归一化后的重复模式在建版本时即被拒绝，因此不存在并列歧义。
5. **跨块状态：** 自动机只吃「已释放」字节。UTF-8 一个多字节字符若落在块边界，前导字节会被**持留**到续接字节到达后再喂入；由于持留的永远是已接收字节的**后缀**，已释放字节仍占据原始流的 `0..n-1` 连续位置，不产生空洞，所以偏移仍是精确的原始字节偏移。
6. **版本不可混用：** 一个扫描构造时绑定某个不可变 `Automaton` 实例；换版本必须显式 `reset`——它替换自动机、回到根节点并把 `epoch + 1`。游标内嵌 epoch，旧游标在新 epoch 上返回 `stale_cursor`(409)。
7. **大扇出可恢复：** 命中在每个 scan 内分配全局单调 `seq`（跨 epoch 唯一），分页是 `seq > after` 的 keyset，HMAC 防篡改；单次投喂产生成千上万命中也能逐页恢复，服务端不在 HTTP 内存里缓冲结果集。
8. **拒绝发生在状态变更之前：** 非法 base64 / 非法编码 / 空模式等在改动任何匹配状态前抛出，因此坏块不会污染偏移。

---

## 3. HTTP 接口速览

| 方法/路径 | 说明 |
|---|---|
| `POST /versions` | 建版本：`{encoding, case_mode, patterns:[base64…]}` → `version_id`、节点数 |
| `GET /versions` / `GET /versions/{id}` | 列/取版本元数据（不含模式明文） |
| `POST /scans` | `{version_id}` 打开扫描 |
| `GET /scans/{id}` | 状态：版本、节点、已消费字节、epoch、命中总数 |
| `POST /scans/{id}/chunks` | `{chunk: base64, version_id?}` 投喂一块（可带版本守卫） |
| `GET /scans/{id}/hits?cursor=&limit=` | 可恢复分页 |
| `POST /scans/{id}/reset` | **显式边界**：换版本/回绕，推进 epoch |
| `POST /scans/{id}/close` | 关闭，拒绝后续投喂 |
| `GET /diagnostics/requests/{request_id}` | 某请求的全部事件 |
| `GET /diagnostics/events?limit=` | 最近事件 |
| `GET /health` | 健康检查 |

每个响应都回 `X-Request-ID`（信任合法的客户端 `X-Request-ID`，否则服务端生成 32 位 hex）。

错误体形状：

```json
{"error": {"code": "empty_pattern", "message": "…",
           "decision": "reject", "details": {"index": 1},
           "request_id": "…"}}
```

稳定错误码：`empty_pattern`、`duplicate_pattern`、`invalid_base64`、
`invalid_encoding`、`unsupported_encoding`、`version_not_found`、
`scan_not_found`、`version_mismatch`(409)、`scan_closed`(409)、
`invalid_cursor`(400)、`stale_cursor`(409)、`limit_out_of_range`(422)、
`chunk_too_large`(422)、`internal_error`(500, **inconclusive**)。

---

## 4. 本地验证命令与「如何判断通过」

需要 Python 3.10+（开发用 3.12.3）。

```bash
# 1) 建虚拟环境并安装锁定依赖
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 2) 运行全部测试（93 个）
.venv/bin/python -m pytest
# 期望：全部 passed；测试包含“与独立朴素实现比对的多重集相等”等强断言，
#       而非仅检查接口可调用。

# 3) 进程内端到端冒烟（会自己建临时库、走真实 ASGI 栈）
.venv/bin/python scripts/smoke.py
# 期望：打印 [1]..[7] 并以 “SMOKE OK — all concrete assertions held.” 结束。

# 4) 打真实 uvicorn 服务器做冒烟
AC_DB_PATH=./data/demo.db .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8011 &
.venv/bin/python scripts/smoke.py --base-url http://127.0.0.1:8011
# 期望：同样 SMOKE OK；其间可：
curl -s http://127.0.0.1:8011/health
```

**判断方式（不是看“绿了”，而是看具体不变量）：**

- `tests/test_automaton.py` 对手工可算的 `he/she/his/hers`、`zabcd→d` 五级输出链、
  `aaaa` 自重叠等断言**精确 (start,end,pattern_id) 集合**；
- `tests/test_streaming.py` / `test_e2e_chunking.py` / `test_fuzz.py` 断言：
  同一逻辑流在**多种分块（含每字节一块/单块/随机块）下命中多重集完全相同**，
  且等于独立 oracle 的结果；并断言 UTF-8 多字节跨块偏移为字节偏移；
- `tests/test_versioning_paging.py` 断言跨版本投喂 409、reset 后 epoch+1、
  旧游标 stale、200 个嵌套后缀在同一点命中时可逐页取完且 seq 连续；
- `tests/test_api_failures.py` 断言每类失败的 **HTTP 状态 + 稳定 code + decision**；
- `tests/test_diagnostics.py` 把 `SECRET-…` 串放进模式/载荷，断言它**不出现在任何
  持久化诊断记录中**，而长度/节点/计数等关键状态在案；
- `test_persistence.py` 模拟进程重启（只留 SQLite、清空热状态），断言跨块命中续接一致。

---

## 5. 依赖版本

| 包 | 版本 | 用途 |
|---|---|---|
| Python | 3.12.3（要求 ≥3.10） | 运行时 |
| fastapi | 0.115.14 | HTTP / 校验 / 依赖注入 |
| starlette | 0.46.2 | 随 fastapi（ASGI、异常处理） |
| uvicorn[standard] | 0.30.6 | 本地服务器 |
| pydantic | 2.13.5 | 模型 |
| pytest | 9.1.1 | 测试 |
| httpx | 0.28.1 | TestClient / live 冒烟 |
| SQLite | 标准库 sqlite3（WAL 模式） | 持久化，无额外服务 |

仅用 Python 标准库实现 AC、HMAC 游标、增量 UTF-8 处理与 oracle，不引入算法第三方库。

---

## 6. 配置（环境变量，`AC_` 前缀）

| 变量 | 默认值 | 含义 |
|---|---|---|
| `AC_DB_PATH` | `./data/ac.db` | SQLite 文件 |
| `AC_SECRET` | `local-dev-secret-change-me` | 游标 HMAC 密钥（本地默认） |
| `AC_MAX_PATTERNS` | 5000 | 单版本模式上限 |
| `AC_MAX_PATTERN_BYTES` | 65536 | 单模式字节上限 |
| `AC_MAX_CHUNK_BYTES` | 4194304 | 单块字节上限 |
| `AC_DEFAULT_PAGE_LIMIT` | 100 | 缺省页大小 |
| `AC_MAX_PAGE_LIMIT` | 1000 | 页大小上限 |

---

## 7. 测试状态（如实标注）

- **已运行且通过：** 全套 93 个 pytest 用例（开发环境 Python 3.12.3）；
  进程内冒烟与 live uvicorn 冒烟均 `SMOKE OK`。
- **未运行：** 无（`pytest` 全量、`scripts/smoke.py` 两种模式都已实际执行）。
- **未通过：** 无遗留失败。开发过程中曾出现并已修复：FastAPI 0.115 对未注解
  依赖参数误判为查询参数、Starlette 0.46 异常处理器签名、整页恰好取完时多发放
  下一页游标、跨 epoch seq 唯一约束、UTF-8 多字节跨块解码等问题。
- **已知非阻塞警告：** starlette `TestClient` 触发的 anyio
  `BlockingPortal` 弃用提示（第三方库自身兼容警告，非本工程代码）。
- **刻意不做：** 多进程自动机热状态共享（当前为单进程 + SQLite；重启可从持久化
  重建续接）、完整 Unicode casefold（会破坏字节偏移）。
