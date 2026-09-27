# 测试黄金向量（外部夹具）

本目录的 `golden_vectors.json` 是**外部生成**的测试夹具，不是被测 Rust 核心
自己算出来的期望值。它用于抓住「测试与实现犯了同一个错误」这类问题。

## 重新生成

```bash
python3 golden_oracle.py        # 写回 golden_vectors.json
python3 golden_oracle.py --print  # 打印到 stdout
```

`golden_oracle.py` 仅依赖 Python 3 标准库（`hashlib` 的 SHA-256，OpenSSL 后端），
与 Rust 实现没有任何共享代码。脚本生成时会先做自身健全性检查（指纹范围、桶号范围、
备用桶定位的对合性）。

## 覆盖

4 个参数集（桶数 16/64/256/1024，指纹 8/12/16 位）× 10 个键
（含空键、ASCII、UTF-8 多字节、emoji、长键、二进制样式串）= 40 个向量，
每行给出固定种子下的 `i1`、`i2`、`fingerprint`。

Rust 侧 `tests/kernel_golden.rs` 读取该文件并逐项断言；若哈希口径被改动，
需要同步修改预言机并重新生成、在评审中说明原因（这会提升 `kernel_version`）。
