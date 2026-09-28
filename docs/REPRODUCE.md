# 复现手册（含正常与异常运行的可核对结果）

所有命令在仓库根目录执行；先：

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.lock && pip install -e .
python scripts/generate_fixtures.py     # 生成 config/ + fixtures/（Ed25519 与 nonce 随机但落盘）
```

> 夹具一旦生成就固定在 `fixtures/*/recording.json`，后续所有复核（测试、oracle、curl）
> 读的是同一份文件，因此结果可重复比对。重新生成会换密钥/PoW nonce，需同步重跑测试。

## A. 离线回放（不启动服务）

```bash
python -m reorgindex.replay.cli replay fixtures/short_fork/recording.json \
    --db run/short_fork.db --report run/reports/short_fork.json
```

期望决策（也写在 `fixtures/short_fork/expected.json`）：

| 区块 | 结果 | 说明 |
|---|---|---|
| g0,m1,m2,m3 | ACCEPT_EXTEND | 主链高度 3，累计权重 16 |
| o1 | **PENDING** | 父 f2 未知，先挂起 |
| f1 | **ACCEPT_SWITCH** | 加权块 16：候选 4+16=20 > 16；**回滚高度区间 [1,3]** |
| f2 | ACCEPT_EXTEND | 延展新链并**级联释放 o1** |

深分叉（最终性拒绝）：

```bash
python -m reorgindex.replay.cli replay fixtures/deep_fork/recording.json \
    --db run/deep_fork.db --report run/reports/deep_fork.json
# d1 = ACCEPT_FORK（权重不足，仅存档）
# d2 = REJECTED, REORG_FINALIZED, would_rollback [1,5]（m1 已有 5 个确认 > K=3）
```

中断与恢复：

```bash
python -m reorgindex.replay.cli replay fixtures/interrupt/recording.json \
    --db run/interrupt.db --report run/reports/interrupt.json
# f1 注入 "after_detach" 崩溃 → 持久化 DETACHED 计划 → 自动恢复完成
```

重建一致性（派生结果 == 当前最佳链全量重建）：

```bash
python -m reorgindex.replay.cli verify fixtures/short_fork/recording.json \
    --db run/short_fork.db
# => "consistent": true, live/rebuilt tip 相同, 事件数相同
```

## B. 真实 HTTP 服务（uvicorn + curl）

```bash
REORG_DB_PATH=run/live.db REORG_LOG_LEVEL=WARNING \
    python -m reorgindex.api.serve     # 监听 127.0.0.1:8080
```

另开终端：

```bash
# 用 python3 从夹具逐块 POST（jq 可选）
python3 - <<'PY'
import json, urllib.request
rec = json.loads(open("fixtures/short_fork/recording.json").read())
for name in rec["arrival_order"]:
    block = next(b["block"] for b in rec["blocks"] if b["name"] == name)
    req = urllib.request.Request(
        "http://127.0.0.1:8080/blocks",
        data=json.dumps(block).encode(), method="POST",
        headers={"Content-Type": "application/json", "X-Request-ID": f"curl-{name}"},
    )
    try:
        body = json.loads(urllib.request.urlopen(req).read())
    except urllib.error.HTTPError as e:   # 异常路径同样是 JSON
        body = json.loads(e.read()); print(name, e.code, body["error"]["code"]); continue
    r = body["result"]
    print(name, r["outcome"], (r.get("switch") or {}).get("rollback_height_range", ""))
PY
```

核对点：

```bash
curl -s http://127.0.0.1:8080/chain | python3 -m json.tool
# weight=28（g0 4 + f1 16 + f2 4 + o1 4），height=3，pending_count=0

# 重复交易：同一 txid 两次出现，只有一个 on_active
TXID=$(python3 -c "import json;print(json.load(open('fixtures/short_fork/recording.json'))['expected']['duplicate_txid'])")
curl -s http://127.0.0.1:8080/transactions/$TXID | python3 -m json.tool

# 最终性/确认深度
TIP_G0=$(curl -s http://127.0.0.1:8080/chain | python3 -c "import json,sys;print(json.load(sys.stdin)['active_hashes'][0])")
curl -s http://127.0.0.1:8080/chain/block/$TIP_G0 | python3 -m json.tool
# => confirmations=4, final=true（4 > K=3）

# 诊断：按请求标识回查为什么接受/拒绝
curl -s "http://127.0.0.1:8080/diagnostics?limit=100" | python3 -m json.tool
```

异常路径示例（深分叉服务内复现）：

```bash
# 依次喂 g0..m5,d1 后提交 d2 → HTTP 422
# 响应体形如：
# {"error": {"code": "REORG_FINALIZED",
#            "message": "reorg would roll back 5 block(s); the shallowest has 5 confirmations ...",
#            "request_id": "...", "state": {"active_tip": "...", "active_height": 5}}}
```

一个**无状态无效**块（改难度）返回 `422 / BAD_DIFFICULTY`；篡改交易签名返回
`422 / BAD_SIGNATURE`；未知父返回 `200 / PENDING`（可在 `/pending` 查看）。

进程内一键示例（不开端口）：

```bash
python scripts/call_service_example.py
```

## C. 测试套件与复核项对照

```bash
python -m pytest -v          # 62 passed
```

| 复核要求 | 测试 |
|---|---|
| 区块按父哈希连接，未知父先挂起 | `test_orphan_*`、`test_unknown_parent_suspends_*`、场景内 o1 |
| 分叉权重固定；短分叉胜出 | `test_short_fork_wins_*`、`test_equal_weight_tie_breaks_*` |
| 深分叉失败 + 失败类别 | `test_deep_fork_is_rejected_*`（断言 `REORG_FINALIZED` 与区间 [1,5]） |
| 切换先撤旧后加新、查询只见完整版本 | `test_switch_detach_then_attach_*`、`test_reader_never_sees_half_written_attach` |
| 切换中断后恢复，输出回滚区间 | `test_crash_after_detach_is_recoverable_*`、`test_switch_interrupted_then_resumed` |
| 重复交易不产生两个有效贡献 | `test_duplicate_txid_*`、`test_same_txid_twice_in_one_chain_*`、场景断言 `active_count==1` |
| 最终性边界内重组明确拒绝；边界外允许 | `test_deep_fork_*`、`test_shallow_switch_just_inside_*`、`test_finality_boundary_exactly_k_*` |
| 派生结果与当前最佳链全量重建一致 | `test_full_rebuild_matches_live_index`、`test_rebuild_projection_matches_oracle` |
| 独立参考答案（非被测核心自产） | `tests/oracle/reference.py`：独立哈希/Merkle/账本/到达模拟；多个场景测试与之全量对账 |
| 断言具体结果与失败类别 | 所有测试断言 outcome/原因码/余额/区间，而非“接口可调” |
| 诊断带请求标识与关键状态；敏感数据脱敏 | `test_diagnostics.py`、API `X-Request-ID` 回查 |

## D. 目录中的可核对产物

* `fixtures/{short_fork,deep_fork,interrupt}/{recording.json,expected.json}` — 最小数据夹具
  与人工预期（最终余额、回滚区间、重复 txid）。
* `run/reports/*.json` — 离线回放报告（运行 CLI 后生成）。
* `requirements.lock` — 完整传递依赖锁定。
