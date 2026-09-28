# 最小数据夹具

全部为本地合成数据，由 `tools/generate_fixtures.py` 生成（**不依赖被测代码**）：

- `keys.json` — 5 把由固定小整数标量派生的合成 secp256k1 密钥（仅测试用）；
- `genesis.json` — 4 枚创世纪 UTXO：
  - `coin_p2pk_a`：P2PK，a，1000，域 `rsv-test-domain-v1`
  - `coin_p2pkh_b`：P2PKH，b，2000，同域
  - `coin_2of3`：2-of-3（a/b/c），3000，同域
  - `coin_domain_b`：P2PK，d，500，域 `rsv-other-domain-v2`
- `bundles/*.json` — 有序交易序列，每笔带独立生成器推出的 `expected` 真值：
  - `happy_path.json`：P2PK + P2PKH + 2-of-3 乱序签名，全部接受；
  - `failure_catalog.json`：重复签名、门槛不足、错误交易域、错误域签名、
    金额不守恒、未知 outpoint；
  - `double_spend.json`：同 outpoint 两连交，先接受后 `state.already_spent`；
- `script_vectors.json` — 43 个栈机级向量（含成熟库交叉校验数据），
  覆盖白名单外操作码、保留字节、最小推送、元素/栈/步数/深度预算、
  栈下溢、IF 分支、终态、四种哈希、哈希锁组合、坏公钥/坏 DER。

重新生成（会覆盖）：

```bash
.venv/bin/python tools/generate_fixtures.py
```
