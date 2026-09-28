# Aho-Corasick 流式匹配后端（acstream）

纯后端服务：对**字节流**做多模式 Aho-Corasick 匹配，支持模式集合版本、
显式边界切换、跨块流式匹配、重叠/后缀命中，以及单点大量命中的可恢复分页。
技术栈：Python 3.12 + FastAPI + SQLite（标准库 `sqlite3`），无外部账号依赖。

---

## 1. 本地验证

```bash
cd opp245/b
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt

# （1）全部测试：151 个，期望全部 passed
python -m pytest -q

# （2）启动服务（默认 127.0.0.1:8080，库文件在 data/acstream.db）
python -m acstream

# （3）另开终端，跑合成夹具演示脚本
bash scripts/demo.sh
```

**预期判断方式**

| 检查 | 预期 |
|---|---|
| `pytest -q` | `151 passed`，退出码 0 |
| `GET /health` | `{"status":"ok",...}` |
| `demo.sh` | 聚合命中数=9（模式 a/aa/aaa 在 `aaaa` 上的全部嵌套重叠）；二进制命中 `[('hdr',2,6),('nul',0,1),('nul',6,7)]`；空模式返回 `EMPTY_PATTERN reject`；脚本末行打印 `✔ 演示完成` |

可用环境变量：`AC_DB_PATH`、`AC_HOST`、`AC_PORT`、`AC_CURSOR_SECRET`
（不设则在数据库目录生成 0600 密钥文件，用于游标 HMAC）、
`AC_DEFAULT_PAGE_SIZE`、`AC_MAX_PAGE_SIZE`、`AC_REDACT_PAYLOADS`。

---

## 2. 算法假设

1. **字节域而非字符域**：模式与文本都是 `bytes`，字母表固定 0..255。
   UTF-8 多字节字符只是若干字节，命中位置是**原始字节偏移**的半开区间
   `[start, end)`，从 0 起。
2. **空模式拒绝**：空字节串没有良定的匹配位置（会在每一个“缝隙”命中），
   构建期以 `EMPTY_PATTERN` 拒绝；空模式集合以 `EMPTY_PATTERN_SET` 拒绝。
3. **全部重叠都报**：在每个终止位置输出该节点经失败链汇总的**所有**终止词，
   包括后缀模式（`she` 同时报 `he`）、自重叠（`aa` in `aaa` 出现两次）和
   前后缀嵌套（`a`/`aa`/`aaa` 同终点各一条）。
4. **跨块无需特殊对齐**：`feed(state, chunk, base_offset)` 只携带一个整数节点
   号与流偏移；模式跨越块边界时由失败转移自然接上，命中位置换算成绝对偏移。
5. **版本切换是显式边界动作**：切换自动机时节点重置为根，**字节偏移不归零**，
   边界本身不插入字节、不产生命中。整数节点号只对构建它的 trie 有意义，
   因此永不跨版本混用；服务端还以 SHA-256 内容指纹做双保险。
6. **版本内容不可变、按内容寻址**：同一模式多重集（与上传顺序无关）指纹相同、
   复用同一 `version_id`；指纹输入为规范化排序后的 `(长度, 字节, id)` 序列。
7. **单进程本地服务假设**：一个 SQLite 连接 + 应用级 `RLock` 串行化请求
   （WAL 模式）。不做水平扩展；多进程部署需另加连接池/迁移机制。
8. 内存复杂度随 trie 节点数线性；节点转移表为完整 256 列确定化表，查询 O(1)
   （构建期一次性补全失败转移）。

### 接受 / 拒绝 / 无法判定

- `accept`：正常接受并处理（会话打开、块消费、版本切换等）。
- `reject`：服务端能确定请求非法——空模式、重复 id、编码错误、结构校验、
  会话不存在、finished 后再喂入等。
- `undetermined`：服务端**无法判定**继续推进是否安全一致——`expected_offset`
  冲突、`expected_fingerprint` 与当前版本不符等。此时当前块**不被消费**，
  客户端核对 `GET /sessions/{sid}/state` 后在正确偏移重试，或先做显式边界切换。

---

## 3. 模块关系（真实职责，非单文件脚本）

```
acstream/
├── config.py            进程配置（环境变量、游标密钥持久化）
├── errors.py            错误码 + ApiError(code,http_status,outcome)
├── text_spec.py         文本规范：utf-8/base64/hex/raw → bytes 的唯一解码口
├── automaton.py         【算法索引】AC trie、失败指针、输出链、feed/search
├── stream.py            【算法状态】StreamMatcher：节点+偏移绑定、边界重置
├── cursor.py            keyset 分页游标的 HMAC 签名/验签（绑定 sid）
├── diagnostics.py       诊断记录与载荷脱敏（bytes 只记长度/可选16字节预览）
├── storage/
│   └── db.py            【版本存储】SQLite schema + 4 个 repository
├── services/
│   ├── version_registry.py  版本创建/幂等复用/重建校验/自动机缓存
│   └── matcher.py           【查询编排】会话、feed、边界、分页、一次性匹配
├── api/
│   ├── schemas.py       Pydantic 请求/响应模型
│   ├── routes.py        HTTP 路由（薄层，只做解码与调服务）
│   ├── deps.py          AppState 装配（连接、服务、锁）
│   └── errors_handler.py 统一错误体 + 拒绝/无法判定事件统一落库
└── app.py / __main__.py 应用装配、X-Request-ID 中间件、uvicorn 入口

tests/                   独立组织的测试（pytest.ini 独立配置）
├── conftest.py          ★独立参考预言机：bytes.find 逐模式朴素搜索 + 分块方案
├── test_automaton.py            失败链/嵌套/二进制/拒绝类别（断言具体错误码）
├── test_streaming_equivalence.py 多分块方案 × 多夹具 ≡ 朴素搜索
├── test_random_oracle.py       60 组随机模式/文本/分块对拍
├── test_pagination.py           大量命中分页完整性 + 游标篡改/跨会话
├── test_version_boundary.py     边界重置语义/偏移保留/指纹守卫
├── test_persistence.py          重启重建自动机、游标跨重启
└── test_api.py                  HTTP 端到端：成功路径与各错误类别的响应
scripts/demo.sh          本地合成夹具演示
```

依赖方向是单向的：`api → services → (automaton/stream/cursor/storage)`，
`diagnostics`/`errors`/`text_spec` 为横切基础模块。算法核心不 import FastAPI，
可离线直接测试。

### 数据表

`pattern_versions`（指纹唯一）、`patterns`（版本内模式字节）、
`sessions`（当前版本、节点号、字节偏移、feed_count、status）、
`hits`（命中流水，keyset 索引 `(sid,end,start,pid,feed_seq)`）、
`diagnostic_events`（request_id/sid/outcome/code/脱敏 key_state）。

---

## 4. HTTP 接口摘要

| 方法 路径 | 说明 |
|---|---|
| `POST /versions` | 创建模式版本（幂等：同内容复用） |
| `GET /versions` | 列出版本 |
| `POST /match` | 无状态一次性匹配（自带模式表） |
| `POST /sessions` | 按版本打开流会话 |
| `POST /sessions/{sid}/feed` | 喂入一块；支持 `expected_offset`/`expected_fingerprint`/`finish` |
| `POST /sessions/{sid}/switch-version` | **显式边界**：切版本、节点回根 |
| `POST /sessions/{sid}/finish` | 结束会话（零字节） |
| `GET /sessions/{sid}/state` | 当前节点/偏移/版本指纹 |
| `GET /sessions/{sid}/hits?limit=&cursor=` | keyset 可恢复分页 |
| `GET /diagnostics?sid=&request_id=&outcome=` | 决策记录（接受/拒绝/无法判定） |
| `GET /health` | 健康检查 |

每个请求可用 `X-Request-ID` 透传请求标识；响应头原样带回，诊断记录按它关联。
错误体统一为：

```json
{"error": {"code": "EMPTY_PATTERN", "message": "...", "request_id": "req_...",
           "outcome": "reject", "details": {"pattern_id": "x"}}}
```

**分页**：`next_cursor` 是签名的 keyset 令牌（排序键
`end ASC, start DESC, pid ASC, feed_seq ASC`——同一终点长词在前）；
末页 `has_more=false` 且无 cursor。令牌绑定 sid、HMAC 防篡改，可在中断后
继续翻页，不依赖行是否仍在原处。

**诊断脱敏**：原始模式/文本字节永不落诊断表，只记长度（默认）或最多 16 字节
hex 预览（`AC_REDACT_PAYLOADS=false`）；结构校验错误不回显输入值。

---

## 5. 依赖版本

Python 3.12（标准库 `sqlite3`、`hashlib`、`hmac`）。虚拟环境实测版本
（`requirements.txt` 锁定）：

| 包 | 版本 | 用途 |
|---|---|---|
| fastapi | 0.141.1 | HTTP 框架 |
| uvicorn | 0.54.0 | 本地 ASGI 服务器 |
| pydantic | 2.13.5 | 请求/响应模型 |
| starlette | 1.7.0（FastAPI 传递依赖） | TestClient/中间件 |
| anyio | 4.15.1（传递依赖） | 线程池调度（会 copy_context 传播 request_id） |
| httpx | 0.28.1 | 仅测试期 TestClient 传输 |
| pytest | 8.4.2 | 测试框架 |

---

## 6. 测试设计要点（参考答案独立性）

- 对照预言机 `tests/conftest.py::naive_find_all` 只用 `bytes.find` 逐模式逐起点
  扫描，**不调用 acstream 任何代码**；被测结果与它比较**排序后的完整多重集**，
  不是只看数量或“接口能调通”。
- 固定夹具：前后缀嵌套、失败链后缀、自重叠周期串、跨块起点、二进制全字节域、
  UTF-8 多字节边界。
- 分块无关性：每份文本在整包/单字节/2字节/质数块/空首块等方案下，命中多重集
  必须恒等于朴素搜索；另有 60 组随机模式×文本×分块对拍。
- 拒绝路径逐项断言具体错误码与 `outcome`（`EMPTY_PATTERN`、
  `EMPTY_PATTERN_SET`、`DUPLICATE_PATTERN_ID`、`ENCODING_ERROR`、
  `VERSION_NOT_FOUND`、`SESSION_NOT_FOUND`、`SESSION_FINISHED`、
  `OFFSET_MISMATCH`、`FINGERPRINT_MISMATCH`、`CURSOR_INVALID`、
  `LIMIT_INVALID`、`VALIDATION_ERROR`）。
- 游标安全：异密钥签名、令牌截断、跨会话复用均必须被拒；会话在“偏移冲突”后
  状态不变且可用正确偏移恢复。
