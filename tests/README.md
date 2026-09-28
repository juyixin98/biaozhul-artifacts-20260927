# 测试与可重放日志

## 组成

| 文件 | 作用 |
|---|---|
| `reference/ref_lz77.py` | **独立第二实现**：暴力编码器 + zlib/hashlib + 独立解码器；也是夹具生成器 |
| `tests/common/mod.rs` | 测试夹具加载、Python 预言机桥接、结构化日志 |
| `tests/oracle_crosscheck.rs` | 双向交叉实现验证、自重叠、跨窗、分块等价 |
| `tests/resource_exhaustion.rs` | 倍率/绝对上限边界、炸弹无巨量分配、链深、类别稳定性 |
| `tests/store_contract.rs` | 缺前块、断链、过期 pin、重载持久性、原子提交 |
| `tests/service_api.rs` | 真实 Axum 路由（in-process）正常路径 + 四类失败状态码 |
| `tests/fixtures/` | 已签入的最小确定性夹具与清单 |
| `scripts/run-tests.sh` | 一键复现入口（重新生成夹具 + 运行 + 聚合日志） |
| `scripts/aggregate_logs.py` | 合并每个 `#[test]` 的日志为 summary.json |

## 日志格式

`tests/test-logs/<run-id>/<suite>/<test-name>.json`：

```json
{
  "run_id": "run-20260927T165128Z-002",
  "run_number": 2,
  "binary": "oracle_crosscheck",
  "test_name": "oracle_crosscheck::...",
  "counts": {"total": 11, "pass": 11, "fail": 0, "skip": 0},
  "cases": [
    {
      "case": "chain_1",
      "judgement": "PASS",
      "reason": "both implementations ... 判定理由 ...",
      "state": { "fixture": "good/chain/1.frame",
                 "rust_category": null, "oracle_category": null,
                 "oracle_stats": {"overlap_copies": 0, "max_distance": 4096} }
    }
  ]
}
```

* **运行编号**：`run-UTC时间戳-NNN`，NNN 在该 checkout 内单调递增；
* **关键中间状态**：夹具路径、声明长度、距离、窗口、两端摘要、双方类别、token 统计、
  RSS 增长等；
* **判断理由**：每条 PASS/FAIL 都有人能读懂的原因，失败时可直接据此重放。

重放单个问题：

1. 在 `summary.json` 找 `failures[]` / 目标 case；
2. 取 `state.fixture`，用参考实现直接复跑：
   ```bash
   python3 reference/ref_lz77.py decode \
     --in tests/fixtures/good/chain/1.frame \
     --dict <前序输出> --report /tmp/r.json
   ```
3. 或只跑对应 Rust 用例：
   ```bash
   LZ77_KEEP_TMP=1 cargo test --test oracle_crosscheck <测试名> -- --nocapture
   ```

## 环境

* `python3`（标准库）缺失时，依赖预言机的用例记 SKIP 而非伪通过；核心 Rust 单测
  与其余套件不受影响。
* `LZ77_KEEP_TMP=1` 保留临时存储目录，便于现场检查。
