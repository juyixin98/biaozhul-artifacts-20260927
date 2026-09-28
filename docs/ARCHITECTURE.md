# 架构与数据流

```
                POST /blocks (FastAPI, X-Request-ID)
                           │
                           ▯
                    ┌──────────────┐   无状态：编码/Merkle/PoW/Ed25519/生产者
                    │  validation  │◄── crypto/{encoding,hashing,keys}
                    └──────┬───────┘
              失败 → IngestionError(RejectReason) ──► diag（含请求标识、状态）
                           │ 通过
                           ▯
        ┌──────────────────┴───────────────────┐
        ▼                                       ▼
 父未知？ → pending_blocks（挂起）         ChainState 重放到分歧点
 父到达 → 级联 pop_pending_children        （nonce/余额/链内 txid）
                                               │
                                               ▯
                                        分叉选择（固定规则）
                          累计权重更大 / 平局取 tip hash 较小
                                               │
                     权重不足 → 存为非活动 fork（ACCEPT_FORK）
                     越过最终性 → REORG_FINALIZED（拒绝，仍存档审计）
                     胜出且合法 → 两阶段切换
                                               │
                ┌──────────────────────────────┴──────────────────────┐
                ▼                                                       ▼
   phase1 begin_detach（提交一次事务）                  phase2 finish_attach（再提交）
   · 写 switch_plan(DETACHED)                            · 写 active_chain/derived_events
   · 逆事件回滚 account_state                            · 正向应用事件
   · 删旧 derived_events/active_chain                    · tx_locations.on_active=1
   · tx_locations.on_active=0                            · 清 switch_plan
        │ （进程在此“被杀”→ 重启 resume_switch 续做）
        ▼
   查询 API 任何时刻只读到完整旧版本或完整新版本（SQLite 事务 + WAL）
```

## 为什么分这四类模块

* **crypto**：只依赖成熟库；规范化编码是唯一的“字节级真相”，签名与 txid 都基于它。
* **kernel**：纯共识/状态规则，可在没有数据库的情况下单测（`test_state.py`、
  `test_validation.py`），也被离线回放复用——在线与离线同一条代码路径。
* **storage**：只管“怎么存/怎么原子撤回”，不含任何共识判断。派生事件行是账本投影的
  最小单位；回滚 = 对旧块事件逐行取逆，保证数学上可对账。
* **replay**：把 `fixtures/*.json` 喂给同一个 kernel，并额外提供“清空后全量重建”
  和“在线库 vs 重建库”逐行对比。

## 两阶段切换与中断恢复

`switch_plan` 是单行表（`id=1`）：

1. `PLANNED→DETACHED` 与旧后缀撤回在**同一事务**提交。崩溃只可能发生在
   “提交前”（无效果）或“提交后”（留下 `DETACHED` 计划）。
2. 服务启动 lifespan 与 `POST /chain/resume` 都会调用 `engine.resume_switch()`：
   读取计划中的 attach 哈希，从已存区块重新派生事件并完成第二阶段，然后删除计划。
   重复执行是幂等的（计划不存在即 no-op；测试显式断言二次 resume 返回 None）。

## 独立 oracle 为什么算“独立”

`tests/oracle/reference.py` 不导入 `reorgindex.kernel` 或 `reorgindex.storage`：
它用自己的一套 Merkle、恒等哈希、账本应用与到达顺序模拟（仅复用 `hashlib/json`），
从夹具原始区块推出最佳链、链哈希、累计权重与每账户余额。测试把**被测系统的存储结果**
与 oracle 的结果逐字段比对；夹具的 `expected.json` 又给出第三份人工预期
（具体余额与回滚区间）。
