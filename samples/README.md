# samples/

合成夹具，无任何真实业务数据。

## golden_vectors.json

由**独立朴素参考实现** `tests/reference/naive_smt.py`（只用 hashlib，不导入
被测内核）通过 `scripts/gen_golden_vectors.py` 生成的金标向量：

* `small_depth8`：1 字节键 / 8 位深的小夹具，手工可读，覆盖单键、共享前缀、
  非成员碰撞、空值等全部形状；包含具体空根与逐字段证明信封。
* `full_depth256`：32 字节键 / 256 位深，两键共享 **255 位**公共前缀
  （只差最后一位），用于长公共前缀与极端压缩路径（256 层单边链）。

测试在运行时断言被测内核根/证明与这些**具体哈希值**逐字节一致。修改哈希协议
（域标签、深度绑定）后必须刻意重新生成：

```bash
.venv/bin/python scripts/gen_golden_vectors.py
```

## 离线流水

`data/journal_export.jsonl` 由 `scripts/export_journal.py` 从播种后的
`data/sample.db` 导出，是离线回放的输入；`data/tampered.jsonl` 为手工篡改
演示（默认不随仓库提供，可按 README 自行构造）。
